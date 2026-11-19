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
       (clear all memory / "本局加载记忆"), "复盘全部"

   Server API (docs/v0.12-contracts.md §10.4):
     GET  api/roster                         PUT  api/roster/{id} {name?, avatar?, persona?, memory?}
     POST api/roster/{id}/avatar:regenerate  POST api/roster/avatars:regenerate
     POST api/roster/{id}/refine {kind, text} -> {draft}
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
    persona: "固定格式的性格与行为指引：定位、说话风格、思考方式、决策偏好、压力下、口头禅。默认为空。",
    memory: "对其他玩家的印象（每人一条）和自己的经验。每局结束后，玩家在复盘时会按本局表现更新印象；这里也可以手动改。",
  };
  // v0.13.1 (G44): what an EMPTY persona box shows - the fixed format that "用主持人模型完善" writes as well
  const PERSONA_TEMPLATE = [
    "【定位】一句话，例如：冷静的逻辑派，喜欢用票型说话。",
    "【说话风格】",
    "- 句子短，先结论后理由。（最多 3 条）",
    "【思考方式】",
    "- 每轮复盘票型，找出跟票异常的人。",
    "【决策偏好】",
    "- 证据不足时宁可弃票，也不乱投。",
    "【压力下】",
    "- 被怀疑时摆出完整时间线，不情绪化。（最多 2 条）",
    "【口头禅】我们看票。（可以留空）",
  ].join("\n");
  const EXPERIENCE_PLACEHOLDER = "每行一条，最多 3 条。例如：第一天先听完警上发言再站边。";
  const NOTES_PLACEHOLDER = "写几个要点（自由文本），例如：N5 爱悍跳；第一天别急着站边。";
  // the limits of docs/v0.12-contracts.md §12.5; GET /api/roster sends the real ones as `limits`
  const DEFAULT_LIMITS = Object.freeze({
    impression_chars: 80,
    experience_items: 3,
    experience_chars: 300,
    memory_chars: 1500,
    legacy_chars: 600,
    persona_chars: 500,
    persona_item_chars: 40,
  });
  const BULLET_RE = /^\s*(?:[-*•·]+|\d{1,2}[.)、．]|[（(]\d{1,2}[）)])\s*/;
  const NO_IMPRESSION = "（暂无）";

  // -------------------------------------------------------------------------
  // pure helpers (tested without a page)
  // -------------------------------------------------------------------------

  /** Characters the way the server counts them (Python's len: an emoji or a Chinese character is one). */
  const textLength = (s) => Array.from(String(s === undefined || s === null ? "" : s)).length;

  /** The limits the server sent; the contract's numbers for whatever is missing. */
  function normalizeLimits(raw) {
    const out = { ...DEFAULT_LIMITS };
    if (raw && typeof raw === "object") {
      for (const k of Object.keys(DEFAULT_LIMITS)) {
        const n = Number(raw[k]);
        if (Number.isFinite(n) && n > 0) out[k] = Math.floor(n);
      }
    }
    return out;
  }

  /** A persona the way the server counts it: line ends normalized, blank lines around it dropped ("" for blank text). */
  function personaText(text) {
    const t = String(text === undefined || text === null ? "" : text).replace(/\r\n?/g, "\n");
    return t.trim() ? t.replace(/^\n+|\n+$/g, "") : "";
  }

  const personaLength = (text) => textLength(personaText(text));

  /** What the server says about a persona that is too long (it refuses the save and cuts nothing), or "". */
  function personaProblem(text, limits) {
    const L = limits || DEFAULT_LIMITS;
    const n = personaLength(text);
    return n > L.persona_chars ? `性格设定最多 ${L.persona_chars} 个字符（现在 ${n} 个），请精简。` : "";
  }

  /** One impression as the server stores it: a single line; the placeholder "（暂无）" means "none yet". */
  function cleanImpression(text) {
    const one = String(text === undefined || text === null ? "" : text).replace(/\s+/g, " ").trim();
    return one === NO_IMPRESSION || one === "(暂无)" || one === "暂无" ? "" : one;
  }

  /** The lessons typed into the box: one per line; bullet marks and empty lines go (cleaned like the server does). */
  function parseExperience(text) {
    return String(text === undefined || text === null ? "" : text)
      .split(/\r\n|\r|\n/)
      .map((line) => cleanImpression(line.replace(BULLET_RE, "")))
      .filter(Boolean);
  }

  /** `## 印象` + `## 经验` as the file holds them: this is what `limits.memory_chars` counts (contract §12.5, detail 6).
   *  others: [{id, name}] in seat order (never the owner); impressions: {id: text}; experience: [text]. */
  function memoryBodyText(others, impressions, experience) {
    const label = (o) => (o.name ? `${o.id} ${o.name}` : o.id);
    const lines = others.map((o) => `- ${label(o)}：${(impressions && impressions[o.id]) || NO_IMPRESSION}`).join("\n");
    const lessons = (experience || []).map((e) => `- ${e}`).join("\n");
    return `## 印象\n${lines}\n\n## 经验${lessons ? `\n${lessons}` : ""}`;
  }

  const memoryChars = (others, impressions, experience) => textLength(memoryBodyText(others, impressions, experience));

  /**
   * The first thing about a memory that is over a limit (the server refuses a save with the same words), or "".
   * others as above; impressions: {id: text} (cleaned); experience: [text]; limits: normalized.
   */
  function memoryProblem(others, impressions, experience, limits) {
    const L = limits || DEFAULT_LIMITS;
    for (const o of others) {
      const n = textLength((impressions && impressions[o.id]) || "");
      if (n > L.impression_chars) return `${o.name ? `${o.id} ${o.name}` : o.id} 的印象最多 ${L.impression_chars} 字（现在 ${n} 字），请精简。`;
    }
    const items = experience || [];
    if (items.length > L.experience_items) return `经验最多 ${L.experience_items} 条（现在 ${items.length} 条），请精简。`;
    const lessons = items.reduce((sum, e) => sum + textLength(e), 0);
    if (lessons > L.experience_chars) return `经验合计最多 ${L.experience_chars} 字（现在 ${lessons} 字），请精简。`;
    const total = memoryChars(others, impressions, items);
    if (total > L.memory_chars) return `整份记忆（印象和经验两段，含序号与符号）最多 ${L.memory_chars} 字（现在 ${total} 字），请精简。`;
    return "";
  }

  /** `memory_parts` of a seat: {record, impressions, experience, legacy}; a server that sends none: the text is "old memory". */
  function normalizeParts(raw, memoryText) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
      return { record: "", impressions: {}, experience: [], legacy: String(memoryText || ""), derived: true };
    }
    const impressions = {};
    if (raw.impressions && typeof raw.impressions === "object") {
      for (const [k, v] of Object.entries(raw.impressions)) impressions[String(k)] = typeof v === "string" ? v : "";
    }
    return {
      record: typeof raw.record === "string" ? raw.record : "",
      impressions,
      experience: Array.isArray(raw.experience) ? raw.experience.map(String) : [],
      legacy: typeof raw.legacy === "string" ? raw.legacy : "",
    };
  }

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
    const lockedSeat = opts.lockedSeat || (() => null); // v0.13: the seat a person plays (the viewpoint is locked to it)
    const patchParticipants = opts.patchParticipants || (() => {});
    const onChange = opts.onChange || (() => {});
    // v0.13.1: the colour palette of the head (marks.js, G16 / G18); "" without it
    const markHeadHtml = opts.markHeadHtml || (() => "");
    // v0.13.1 (G21, G25): the thinking record of an agent; `thoughtsBlocked(id)` is the reason it cannot be opened now ("" = it can)
    const openThoughts = opts.openThoughts || (() => {});
    const thoughtsBlocked = opts.thoughtsBlocked || (() => "");
    // a window of the page (the thinking record, the error log) is open: Escape closes that first, not the panel behind it
    const isWindowOpen = opts.isWindowOpen || (() => false);
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
      btnClear: $("btn-roster-clear"),
      chkMemory: $("chk-memory-enabled"),
      btnReviewAll: $("btn-review-all"),
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
      limits: normalizeLimits(null), // v0.13.1: GET /api/roster `limits` (the persona and memory limits)
      seats: new Map(), // id -> {seat, id, name, default_name, avatar, persona, memory, parts: {record, impressions, experience, legacy}}
      sessionKey: null,
      openId: null,
      drafts: {}, // "N3:persona" -> text typed in the editor (unsaved)
      // v0.13.1 (G42): the memory editor is structured: "N3" -> {imp: {N5: "typed text"}, exp: "typed lessons"} - only what was touched
      memDraft: {},
      memNotes: {}, // "N3" -> the notes typed for "用主持人模型完善" (free text, not saved)
      refine: {}, // "N3:persona" / "N3:memory" -> {busy, draft, parts?, fromLegacy?}
      saving: {}, // "N3:persona" / "name:N3" / "regen:N3" / "avatar:N3" -> true
      nameDraft: {}, // id -> typed name
      nameError: {},
      expanded: {}, // "N3:persona" -> true
      // v0.13.1 (G28-G31): the 🎲 panel of the avatar - open flag, the typed code and what was said about it, per seat
      avatarOpen: {},
      codeDraft: {},
      codeNote: {}, // id -> {kind: "error" | "hint" | "ok", text}
      changesOpen: {}, // id -> false when the user folded the list of what the review changed
      lastHead: null,
      lastLive: null,
      lastEditors: null,
      busyAll: false,
      busyReview: false,
      // v0.13.1: answers that cross.  A request is numbered when it is SENT; of two answers that cross, only the one of the LATER request may
      // overwrite what the other wrote (a seat, or the roster's switches): an older answer must not roll the view back
      rev: 0,
      seatRev: {}, // id -> the request whose answer wrote that seat last
      metaRev: 0, // ... and the roster's switches (limits, editable, memory_enabled)
      // a refine request that is still on its way when the text it was made from stops being the text (a save, a review that overwrote
      // the memory, a new game, "clear all memory") must not bring its draft back: its answer is stale when its generation is not current
      refineGen: {}, // "N3:memory" -> generation
      refineEpoch: 0, // a new game / "clear all memory": every key at once
    };

    const session = () => getSession() || {};
    // (a replayed game - session.replay - is never editable: the roster belongs to the live game)
    const isIdle = () => session().status === "idle" && !session().replay;
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

    /** Is the keyboard focus inside `node` (a text box of it)?  (False under the test DOM, which has no contains().) */
    function hasFocus(node) {
      const active = doc.activeElement;
      return Boolean(active && node && typeof node.contains === "function" && node.contains(active));
    }

    // -----------------------------------------------------------------------
    // roster data
    // -----------------------------------------------------------------------

    /** Numbers a request when it is SENT (see `st.rev`). */
    const sent = () => ++st.rev;

    /** `ticket`: the number of the request this answer belongs to (a caller that did not number its request: the newest one). */
    function applyRoster(resp, ticket) {
      const t = ticket === undefined ? sent() : ticket;
      const r = Core.normalizeRoster(resp);
      // v0.13.1: the structured memory (`memory_parts`) and the limits ride along (Core.normalizeRoster keeps neither)
      const rawById = new Map((resp && Array.isArray(resp.seats) ? resp.seats : []).filter((x) => x && x.id).map((x) => [String(x.id), x]));
      const before = st.seats;
      st.seats = new Map(
        r.seats.map((s) => {
          const kept = before.get(s.id);
          // a LATER request has written this seat since this one was sent: what this answer says of it is older than what we hold
          if (kept && (st.seatRev[s.id] || 0) > t) return [s.id, kept];
          st.seatRev[s.id] = t;
          const raw = rawById.get(s.id) || {};
          // an answer without `memory_parts` whose memory text is the one we already hold (a partial answer: names and avatars)
          // keeps the structure we had; anything else (an older server) reads the structure out of the text
          const parts =
            raw.memory_parts === undefined && kept && kept.parts && kept.memory === s.memory ? kept.parts : normalizeParts(raw.memory_parts, s.memory);
          return [s.id, { ...s, parts }];
        })
      );
      if (t > st.metaRev) {
        if (resp && resp.limits && typeof resp.limits === "object") st.limits = normalizeLimits(resp.limits);
        st.editable = r.editable;
        st.memoryEnabled = r.memory_enabled;
        st.metaRev = t;
      }
      st.loaded = true;
      st.available = true;
      return r;
    }

    /** GET /api/roster. A missing endpoint (older backend) just disables the editors. */
    async function load() {
      try {
        const ticket = sent();
        const res = await api.get("api/roster");
        applyRoster(res, ticket);
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

    /**
     * One seat's entry out of a write response (the object itself, or {seat:{…}}, or {seats:[…]}).  Returns false when there is nothing
     * to merge (the caller reads the roster again), true when the answer was taken - or left out because a LATER request has written the
     * seat since this one was sent (`ticket`: its number): such an answer is older than what we hold and there is nothing to re-read either.
     */
    function mergeSeatResponse(id, res, ticket) {
      if (!res || typeof res !== "object") return false;
      const one = res.seat && typeof res.seat === "object" ? res.seat : Array.isArray(res.seats) ? res.seats.find((x) => x && x.id === id) : res;
      if (!one || (one.id !== undefined && one.id !== id)) return false;
      // {ok: true} and friends carry nothing to merge: the caller reads the roster again
      if (!["name", "avatar", "persona", "memory", "memory_parts"].some((k) => one[k] !== undefined && one[k] !== null)) return false;
      const t = ticket === undefined ? sent() : ticket;
      if ((st.seatRev[id] || 0) > t) return true;
      st.seatRev[id] = t;
      const cur = st.seats.get(id) || { seat: 0, id, name: id, default_name: "", avatar: null, persona: "", memory: "", parts: normalizeParts({}, "") };
      const next = { ...cur };
      if (typeof one.name === "string") next.name = one.name;
      if (one.avatar && typeof one.avatar === "object") next.avatar = one.avatar;
      if (typeof one.persona === "string") next.persona = one.persona;
      if (typeof one.memory === "string") next.memory = one.memory;
      if (one.memory_parts && typeof one.memory_parts === "object") next.parts = normalizeParts(one.memory_parts, next.memory);
      st.seats.set(id, next);
      const patch = { id };
      if (typeof one.name === "string") patch.name = one.name;
      if (one.avatar && typeof one.avatar === "object") patch.avatar = one.avatar;
      if (patch.name !== undefined || patch.avatar !== undefined) patchParticipants([patch]);
      return true;
    }

    /** After a write: use what the server returned, otherwise read the roster again. */
    async function afterWrite(id, res, ticket) {
      if (!mergeSeatResponse(id, res, ticket)) await load();
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

    // ---- v0.13.1 (G28-G31): the avatar CODE under the avatar, "复制", and the 🎲 panel ----------------------------------

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
      // (no `trim()` of our own: the decoder says "empty" itself, by the rules the backend shares - tests/avatar_code_cases.json -
      // and JS's idea of white space is not those rules: it strips a lone U+1680, which the shared cases call bad_format)
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
    function avatarRowHtml(id, desc, canEdit) {
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
            ` aria-expanded="${open}" title="重新生成头像：随机，或输入编号（对局开始后不能改）">🎲</button>`
          : "") +
        `</div>${open ? avatarPopHtml(id) : ""}`
      );
    }

    // ---- v0.13.1 (G21, G25): "🧠 思考记录" ------------------------------------------------------------------------

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

    function headHtml(id) {
      const s = session();
      if (id === MOD) {
        const name = (s.moderator && s.moderator.name) || "主持人";
        // the moderator has a random pixel avatar of its own (the fixed top hat only when the server sent none)
        const avatar = s.moderator && s.moderator.avatar ? s.moderator.avatar : null;
        // (a backend that sends no moderator avatar does not have the endpoint either: no 🎲 then)
        const canRegen = Boolean(avatar) && isIdle() && st.editable && st.available;
        return (
          `<div class="detail-head-row">` +
          `<div class="detail-avatar">${Avatar.imgHtml(MOD, undefined, avatar, { size: 80 })}</div>` +
          `<div class="detail-title"><div class="detail-name">${esc(name)}</div>` +
          `<div class="detail-sub">主持人</div></div>` +
          `<button type="button" class="pop-close" data-detail-action="close" aria-label="关闭">✕</button></div>` +
          avatarRowHtml(MOD, avatar, canRegen) +
          `<div class="detail-actions">${thoughtsActionHtml(MOD)}</div>` +
          thoughtsNoteHtml(MOD)
        );
      }
      const p = View.participantOf(s, id);
      if (!p) return null;
      const editable = isIdle() && st.editable && st.available;
      const nameBusy = Boolean(st.saving[`name:${id}`]);
      const err = st.nameError[id] || "";
      const nameBlock = editable
        ? `<div class="detail-name-row"><input type="text" class="form-input detail-name-input" id="input-detail-name"` +
          ` value="${esc(nameValue(p))}" aria-label="名字（1–20 个字符，不能和其他玩家重名）" autocomplete="off" spellcheck="false" />` +
          `<button type="button" class="btn btn-xs btn-primary-outline" id="btn-detail-save-name" data-detail-action="save-name"` +
          `${nameBusy ? " disabled" : ""}>保存名字</button></div>` +
          `<div class="form-error detail-error"${err ? "" : " hidden"}>${esc(err)}</div>`
        : `<div class="detail-name" title="对局未开始时才能改名">${esc(p.name)}</div>`;
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
        avatarRowHtml(id, p.avatar, editable) +
        `<div class="detail-actions">${actions.join("")}</div>` +
        thoughtsNoteHtml(id) +
        markHeadHtml(id) // v0.13.1 (G16 / G18): the colour palette under the actions (marks.js)
      );
    }

    // -----------------------------------------------------------------------
    // detail panel: live part (state, tags, model, metrics, review)
    // -----------------------------------------------------------------------

    function statHtml(value, label, extra) {
      return `<div class="pop-stat${extra || ""}"><span class="pop-stat-num">${value}</span><span class="pop-stat-label">${esc(label)}</span></div>`;
    }

    /** "3 号 Carol" (the live nickname), escaped. */
    function seatLabelOf(id) {
      const p = View.participantOf(session(), id);
      return p ? `${Core.numText(p.seat)} 号 ${esc(p.name)}` : esc(id);
    }

    /**
     * v0.13.1 (G40): what a finished review changed in the memory - the old impression and the new one of every seat
     * that changed, and the lessons.  It comes with the `review.progress` event (the client keeps it while the page lives).
     */
    function changesHtml(id) {
      const s = session();
      const progress = s.review && s.review.progress ? s.review.progress[id] : null;
      const ch = progress && progress.changes;
      if (!ch) return "";
      const n = ch.impressions.length;
      const exp = ch.experience;
      if (!n && !exp) {
        return `<div class="review-changes"><div class="review-changes-head">本次复盘没有改动印象和经验（战绩已计入）。</div></div>`;
      }
      const open = st.changesOpen[id] !== false;
      const summary = `本次改动：${n ? `${Core.numText(n)} 条印象` : "印象没有变化"}${exp ? "，经验已更新" : ""}（旧 → 新）`;
      const items = ch.impressions.map(
        (c) =>
          `<li class="rc-item"><div class="rc-who">${seatLabelOf(c.id)}</div>` +
          `<div class="rc-line rc-before"><span class="rc-tag">旧</span><span>${esc(c.before) || NO_IMPRESSION}</span></div>` +
          `<div class="rc-line rc-after"><span class="rc-tag new">新</span><span>${esc(c.after) || "（已清空）"}</span></div></li>`
      );
      if (exp) {
        const text = (list) => esc(list.join("；")) || NO_IMPRESSION;
        items.push(
          `<li class="rc-item"><div class="rc-who">我的经验</div>` +
            `<div class="rc-line rc-before"><span class="rc-tag">旧</span><span>${text(exp.before)}</span></div>` +
            `<div class="rc-line rc-after"><span class="rc-tag new">新</span><span>${text(exp.after)}</span></div></li>`
        );
      }
      return (
        `<div class="review-changes"><button type="button" class="review-changes-toggle" data-detail-action="toggle-changes" aria-expanded="${open}">` +
        `<span class="caret" aria-hidden="true">${open ? "▼" : "▶"}</span>${summary}</button>` +
        (open ? `<ul class="review-changes-list">${items.join("")}</ul>` : "") +
        `</div>`
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
            // (v0.13.1, G39 / G40: the review REVISES the impressions and the lessons; the file is overwritten, nothing is appended)
            review = `<div class="detail-review done"><span class="review-badge done">✓ 已复盘</span>` +
              `<button type="button" class="btn btn-xs" data-detail-action="show-memory">查看写入的记忆</button>` +
              `<span class="form-hint">战绩已计入，对其他玩家的印象已按本局更新。</span></div>` +
              changesHtml(id);
          } else if (one.state === "running") {
            review = `<div class="detail-review"><span class="review-badge running">⏳ 复盘中…</span>${one.message ? `<span class="form-hint">${esc(one.message)}</span>` : ""}</div>`;
          } else {
            review =
              `<div class="detail-review"><button type="button" class="btn btn-xs btn-primary-outline" data-detail-action="review-one"` +
              `${rv.available ? "" : " disabled"}>复盘此玩家</button>` +
              `${one.state === "error" ? `<span class="form-hint error">上次复盘失败：${esc(one.message)}</span>` : `<span class="form-hint">让该玩家用自己的模型复盘本局，更新对其他玩家的印象。</span>`}</div>`;
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

    /** The proposal of the moderator's model (both kinds): read it first, only "采用并保存" saves it. */
    function draftBlockHtml(kind, rf, saving) {
      if (rf.draft === undefined || rf.draft === null) return "";
      return (
        `<div class="detail-draft"><div class="detail-draft-title">主持人模型给出的草稿（尚未保存，请先看一遍）</div>` +
        `<div class="detail-draft-text">${esc(rf.draft)}</div>` +
        (kind === "memory"
          ? `<div class="form-hint">采用后，上面的印象和经验会换成这份草稿（还没保存的修改也会被覆盖）。${rf.fromLegacy ? "旧记忆段也会一并删除（它的内容已经整理进印象和经验）。" : ""}</div>`
          : "") +
        `<div class="detail-editor-actions">` +
        `<button type="button" class="btn btn-xs btn-primary-outline" data-detail-action="accept-draft" data-kind="${kind}"${saving ? " disabled" : ""}>采用并保存</button>` +
        `<button type="button" class="btn btn-xs" data-detail-action="discard-draft" data-kind="${kind}">放弃草稿</button></div></div>`
      );
    }

    function editorFrameHtml(id, kind, meta, bodyHtml) {
      const open = Boolean(st.expanded[keyOf(id, kind)]);
      return (
        `<section class="detail-editor${open ? " open" : ""}" data-kind="${kind}">` +
        `<button type="button" class="detail-editor-toggle" data-detail-action="toggle-editor" data-kind="${kind}" aria-expanded="${open}">` +
        `<span class="caret" aria-hidden="true">${open ? "▼" : "▶"}</span><span class="detail-editor-title">${KIND_LABELS[kind]}</span>` +
        `<span class="detail-editor-meta">${esc(meta)}</span></button>` +
        `<div class="detail-editor-body">${bodyHtml}</div></section>`
      );
    }

    // ---- 性格设定 (G43, G44): the fixed format, the template as the placeholder, "x / 500", refused above the limit ----------

    function personaState(id) {
      const text = currentText(id, "persona");
      const saved = savedText(id, "persona");
      const limit = st.limits.persona_chars;
      const n = personaLength(text);
      return { text, saved, limit, n, over: n > limit, dirty: personaText(text) !== personaText(saved) };
    }

    function personaEditorHtml(id) {
      const kind = "persona";
      const k = keyOf(id, kind);
      const p = personaState(id);
      const saving = Boolean(st.saving[k]);
      const rf = st.refine[k] || {};
      const usable = st.available && st.loaded;
      const savedN = personaLength(p.saved);
      const meta = !p.saved ? "空" : savedN > p.limit ? `⚠ 超出上限 ${savedN} / ${p.limit}` : `已设置 · ${Core.charLength(p.saved)} 字`;
      return editorFrameHtml(
        id,
        kind,
        meta,
        `<div class="form-hint">${esc(KIND_HINTS[kind])}${isIdle() ? "" : "　对局进行中修改的内容从下一局开始生效。"}</div>` +
          `<textarea class="form-input detail-textarea" id="ta-detail-persona" data-draft-key="${esc(k)}" rows="9"` +
          ` placeholder="${esc(PERSONA_TEMPLATE)}"${usable ? "" : " disabled"}>${esc(p.text)}</textarea>` +
          `<div class="detail-count-row"><span class="detail-count${p.over ? " over" : ""}" id="count-detail-persona">${p.n} / ${p.limit}</span>` +
          `<span class="form-hint error" id="hint-detail-persona" role="status"${p.over ? "" : " hidden"}>超出上限，请精简或用主持人模型整理</span></div>` +
          `<div class="detail-editor-actions">` +
          `<button type="button" class="btn btn-xs" id="btn-detail-refine-persona" data-detail-action="refine" data-kind="persona"` +
          `${usable && !rf.busy ? "" : " disabled"} title="把上面的要点交给主持人绑定的模型，整理成固定格式的性格指引；结果先给你确认再保存">${rf.busy ? "完善中…" : "✨ 用主持人模型完善"}</button>` +
          `<button type="button" class="btn btn-xs btn-primary-outline" id="btn-detail-save-persona" data-detail-action="save-text" data-kind="persona"` +
          `${usable && p.dirty && !saving && !p.over ? "" : " disabled"}>${saving ? "保存中…" : "保存"}</button></div>` +
          `${usable ? "" : `<div class="form-hint error">名册接口不可用（后端可能尚未升级），暂时不能编辑。</div>`}` +
          draftBlockHtml(kind, rf, saving)
      );
    }

    // ---- 记忆 (G37-G42): the record, an impression per other seat (one line each), the lessons, the old text to tidy up ----------

    /** The other seats in seat order: [{id, name, seat, avatar}] (the memory keeps an impression of each, never of the owner). */
    function othersOf(id) {
      return roster()
        .filter((x) => x.id !== id)
        .sort((a, b) => a.seat - b.seat)
        .map((x) => {
          const live = View.participantOf(session(), x.id);
          return { id: x.id, name: x.name, seat: x.seat, avatar: live && live.avatar ? live.avatar : x.avatar };
        });
    }

    /** The memory editor's numbers: what is saved, what is typed over it, the cleaned values, the counts and the first problem. */
    function memoryState(id) {
      const seat = seatOf(id);
      const parts = seat && seat.parts ? seat.parts : normalizeParts({}, "");
      const others = othersOf(id);
      const draft = st.memDraft[id] || {};
      const typed = {};
      const cleaned = {};
      for (const o of others) {
        typed[o.id] = draft.imp && draft.imp[o.id] !== undefined ? draft.imp[o.id] : parts.impressions[o.id] || "";
        cleaned[o.id] = cleanImpression(typed[o.id]);
      }
      const expText = draft.exp !== undefined ? draft.exp : parts.experience.join("\n");
      const lessons = parseExperience(expText);
      const L = st.limits;
      const dirty =
        others.some((o) => cleaned[o.id] !== cleanImpression(parts.impressions[o.id] || "")) ||
        JSON.stringify(lessons) !== JSON.stringify(parts.experience);
      return {
        parts,
        others,
        typed,
        cleaned,
        expText,
        lessons,
        lessonChars: lessons.reduce((sum, e) => sum + textLength(e), 0),
        total: memoryChars(others, cleaned, lessons),
        problem: memoryProblem(others, cleaned, lessons, L),
        dirty,
        filled: others.filter((o) => cleanImpression(parts.impressions[o.id] || "")).length,
      };
    }

    function memoryEditorHtml(id) {
      const kind = "memory";
      const k = keyOf(id, kind);
      const m = memoryState(id);
      const L = st.limits;
      const saving = Boolean(st.saving[k]);
      const rf = st.refine[k] || {};
      const usable = st.available && st.loaded;
      const legacy = m.parts.legacy.trim();
      const record = m.parts.record.trim();
      const empty = !record && !legacy && !m.filled && !m.parts.experience.length;
      const meta = empty ? "空" : `印象 ${m.filled}/${m.others.length} · ${m.total} / ${L.memory_chars} 字${legacy ? " · 有旧记忆待整理" : ""}`;
      const rows = m.others
        .map((o) => {
          const n = textLength(m.cleaned[o.id]);
          // (two lines per seat: who and the counter, then the box across the whole width - an impression can be 80 characters
          // and the panel is narrow; the box is a <textarea> for that reason, Enter is blocked: an impression is one line)
          return (
            `<li class="mem-row" data-imp-row="${esc(o.id)}"><div class="mem-head"><span class="mem-who">${Avatar.imgHtml(o.id, o.seat, o.avatar, { size: 20, className: "mem-avatar" })}` +
            `<span class="mem-name">${Core.numText(o.seat)} 号 ${esc(o.name)}</span></span>` +
            `<span class="mem-count${n > L.impression_chars ? " over" : ""}" id="count-imp-${esc(o.id)}">${n} / ${L.impression_chars}</span></div>` +
            `<textarea class="form-input mem-input" data-imp-id="${esc(o.id)}" rows="2" placeholder="${NO_IMPRESSION}"` +
            ` autocomplete="off" spellcheck="false" aria-label="对 ${esc(o.name)} 的印象（最多 ${L.impression_chars} 字）"${usable ? "" : " disabled"}>${esc(m.typed[o.id])}</textarea></li>`
          );
        })
        .join("");
      const legacyBlock = legacy
        ? `<div class="mem-legacy" id="mem-legacy"><div class="mem-legacy-head">⚠ 旧记忆（待整理）<span class="form-hint">${Core.numText(textLength(legacy))} / ${L.legacy_chars} 字</span></div>` +
          `<div class="mem-legacy-text">${esc(legacy)}</div>` +
          `<div class="form-hint">这是旧版本留下的记忆。下次复盘时，该玩家会把它整理进印象和经验，之后这一段会被删除；也可以现在点「用主持人模型整理」立即转换。</div>` +
          `<div class="detail-editor-actions"><button type="button" class="btn btn-xs" id="btn-detail-tidy-legacy" data-detail-action="tidy-legacy"` +
          `${usable && !rf.busy ? "" : " disabled"}>${rf.busy && rf.fromLegacy ? "整理中…" : "✨ 用主持人模型整理"}</button>` +
          `<button type="button" class="btn btn-xs btn-danger-outline" id="btn-detail-drop-legacy" data-detail-action="drop-legacy"${usable && !saving ? "" : " disabled"}>删除旧记忆</button></div></div>`
        : "";
      return editorFrameHtml(
        id,
        kind,
        meta,
        `<div class="form-hint">${esc(KIND_HINTS[kind])}${isIdle() ? "" : "　对局进行中修改的内容从下一局开始生效。"}</div>` +
          (record
            ? `<div class="mem-record" title="战绩由引擎计算，不能在这里改"><span class="mem-record-label">战绩</span><span class="mem-record-text">${esc(record)}</span></div>`
            : "") +
          legacyBlock +
          `<div class="mem-section-title">对其他玩家的印象<span class="form-hint">每条最多 ${L.impression_chars} 字，写风格、习惯、可信度，不写“他是狼 / 预言家”（身份每局重新分配）</span></div>` +
          `<ul class="mem-rows">${rows}</ul>` +
          `<div class="mem-section-title">我的经验<span class="form-hint">每行一条，最多 ${L.experience_items} 条，合计 ${L.experience_chars} 字</span></div>` +
          `<textarea class="form-input detail-textarea mem-exp" id="ta-detail-memory-exp" rows="3" placeholder="${esc(EXPERIENCE_PLACEHOLDER)}"${usable ? "" : " disabled"}>${esc(m.expText)}</textarea>` +
          `<div class="mem-counts"><span id="count-detail-exp" class="${m.lessons.length > L.experience_items || m.lessonChars > L.experience_chars ? "over" : ""}">` +
          `${m.lessons.length} / ${L.experience_items} 条 · ${m.lessonChars} / ${L.experience_chars} 字</span>` +
          `<span id="count-detail-total" class="mem-total${m.total > L.memory_chars ? " over" : ""}" title="印象和经验两段的全部文字，含序号与符号">整份 ${m.total} / ${L.memory_chars} 字</span></div>` +
          `<div class="form-hint error" id="hint-detail-memory" role="status"${m.problem ? "" : " hidden"}>${esc(m.problem)}</div>` +
          `<div class="detail-editor-actions"><button type="button" class="btn btn-xs btn-primary-outline" id="btn-detail-save-memory" data-detail-action="save-text" data-kind="memory"` +
          `${usable && m.dirty && !saving && !m.problem ? "" : " disabled"}>${saving ? "保存中…" : "保存"}</button></div>` +
          `<div class="mem-refine"><div class="mem-section-title">让主持人模型帮忙<span class="form-hint">把要点并进上面的印象和经验；结果先给你确认再保存</span></div>` +
          `<textarea class="form-input detail-textarea mem-notes" id="ta-detail-memory-notes" rows="2" placeholder="${esc(NOTES_PLACEHOLDER)}"${usable ? "" : " disabled"}>${esc(st.memNotes[id] || "")}</textarea>` +
          `<div class="detail-editor-actions"><button type="button" class="btn btn-xs" id="btn-detail-refine-memory" data-detail-action="refine" data-kind="memory"` +
          `${usable && !rf.busy ? "" : " disabled"} title="把上面的要点交给主持人绑定的模型，整理进印象和经验；结果先给你确认再保存">${rf.busy && !rf.fromLegacy ? "完善中…" : "✨ 用主持人模型完善"}</button></div></div>` +
          `${usable ? "" : `<div class="form-hint error">名册接口不可用（后端可能尚未升级），暂时不能编辑。</div>`}` +
          draftBlockHtml(kind, rf, saving)
      );
    }

    function editorsHtml(id) {
      if (id === MOD) return "";
      return personaEditorHtml(id) + memoryEditorHtml(id);
    }

    // ---- what typing changes without a repaint (the caret must stay where it is) -------------------------------------

    function setText(node, text, className) {
      if (!node) return;
      node.textContent = text;
      if (className !== undefined) node.className = className;
    }

    function updatePersonaLive(id) {
      const p = personaState(id);
      const saving = Boolean(st.saving[keyOf(id, "persona")]);
      setText($("count-detail-persona"), `${p.n} / ${p.limit}`, `detail-count${p.over ? " over" : ""}`);
      const hint = $("hint-detail-persona");
      if (hint) hint.hidden = !p.over;
      const btn = $("btn-detail-save-persona");
      if (btn) {
        btn.disabled = !(p.dirty && !saving && !p.over);
        btn.textContent = saving ? "保存中…" : "保存";
      }
    }

    function updateMemoryLive(id) {
      const m = memoryState(id);
      const L = st.limits;
      const saving = Boolean(st.saving[keyOf(id, "memory")]);
      for (const o of m.others) {
        const n = textLength(m.cleaned[o.id]);
        setText($(`count-imp-${o.id}`), `${n} / ${L.impression_chars}`, `mem-count${n > L.impression_chars ? " over" : ""}`);
      }
      setText($("count-detail-exp"), `${m.lessons.length} / ${L.experience_items} 条 · ${m.lessonChars} / ${L.experience_chars} 字`,
        m.lessons.length > L.experience_items || m.lessonChars > L.experience_chars ? "over" : "");
      setText($("count-detail-total"), `整份 ${m.total} / ${L.memory_chars} 字`, `mem-total${m.total > L.memory_chars ? " over" : ""}`);
      const hint = $("hint-detail-memory");
      if (hint) {
        hint.textContent = m.problem;
        hint.hidden = !m.problem;
      }
      const btn = $("btn-detail-save-memory");
      if (btn) {
        btn.disabled = !(m.dirty && !saving && !m.problem);
        btn.textContent = saving ? "保存中…" : "保存";
      }
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
      // (v0.13.1) a repaint that nobody asked for - an event, the 1 s ticker - must not replace the text box the user is
      // typing in (its caret and focus would be lost): such a region waits until the focus leaves it
      const asked = (part) => Boolean(only && only[part]);
      if (want.head && (asked("head") || !hasFocus(el.head))) setHtmlIfChanged(el.head, head, "lastHead");
      if (want.live) setHtmlIfChanged(el.live, liveHtml(st.openId) || "", "lastLive");
      if (want.editors && (asked("editors") || !hasFocus(el.editors))) setHtmlIfChanged(el.editors, editorsHtml(st.openId), "lastEditors");
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
      // An unchanged name is nothing to save - whatever it is.  Only a NEW nickname is held to the rules: an old name that the server
      // would not accept today (one with a colon) still loads, still shows everywhere, and pressing Save / Enter on it does nothing.
      if (String(raw === undefined || raw === null ? "" : raw).trim() === p.name) {
        st.nameError[id] = "";
        delete st.nameDraft[id];
        render({ head: true });
        return;
      }
      const others = (session().participants || []).filter((x) => x.id !== id).map((x) => x.name);
      const checked = Core.validateNickname(raw, others);
      if (!checked.ok) {
        st.nameError[id] = checked.error;
        st.nameDraft[id] = raw;
        render({ head: true });
        return;
      }
      const typed = st.nameDraft[id]; // what the box held when Save was pressed
      st.saving[`name:${id}`] = true;
      st.nameError[id] = "";
      render({ head: true });
      try {
        const ticket = sent();
        const res = await api.put(`api/roster/${enc(id)}`, { name: checked.value });
        // what was typed while the request was on its way is a LATER edit: it stays (an unsaved difference to the name saved now)
        if (st.nameDraft[id] === typed) delete st.nameDraft[id];
        // the response, else the SSE roster.updated, else a re-read keeps every view in step
        if (!mergeSeatResponse(id, res, ticket)) patchParticipants([{ id, name: checked.value }]);
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
      st.codeNote[id] = null;
      render({ head: true });
      try {
        const ticket = sent();
        const res = await api.post(`api/roster/${enc(id)}/avatar:regenerate`);
        if (id === MOD) {
          // the moderator is not a seat: its new avatar goes straight to the session's moderator
          if (res && res.avatar && typeof res.avatar === "object") patchParticipants([{ id: MOD, avatar: res.avatar }]);
          else await load();
          render();
          onChange();
        } else {
          await afterWrite(id, res, ticket);
        }
        st.codeNote[id] = { kind: "ok", text: "已随机生成新头像。" };
        showToast(id === MOD ? "主持人头像已重新生成" : "头像已重新生成");
      } catch (e) {
        st.codeNote[id] = { kind: "error", text: e.detail || e.message };
        showToast(e.message);
      } finally {
        delete st.saving[`regen:${id}`];
        render({ head: true });
      }
    }

    // ---- v0.13.1 (G28-G31): the avatar code: copy it, make an avatar from a code ---------------------------------------

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
      if (note) setText(note, st.codeNote[id] ? st.codeNote[id].text : "", `avatar-pop-note${st.codeNote[id] ? ` ${st.codeNote[id].kind}` : ""}`);
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
        const ticket = sent();
        const res = await api.put(`api/roster/${enc(id)}`, { avatar: check.description });
        if (id === MOD) {
          patchParticipants([{ id: MOD, avatar: res && res.avatar && typeof res.avatar === "object" ? res.avatar : check.description }]);
          render();
          onChange();
        } else {
          await afterWrite(id, res, ticket);
        }
        const code = Avatar.codeOf(check.description);
        st.codeDraft[id] = "";
        st.codeNote[id] = check.twins.length
          ? { kind: "hint", text: `头像已按编号生成。注意：和 ${check.twins.join("、")} 的头像完全相同（同款）。` }
          : { kind: "ok", text: "头像已按编号生成。" };
        showToast(check.twins.length ? `头像已按编号生成：${code}（和 ${check.twins.join("、")} 同款）` : `头像已按编号生成：${code}`);
      } catch (e) {
        st.codeNote[id] = { kind: "error", text: e.detail || e.message || "生成失败" };
      } finally {
        delete st.saving[`avatar:${id}`];
        render({ head: true });
      }
    }

    // ---- saving: the persona (free text under a limit) and the memory (structured parts) ------------------------------

    async function saveText(kind, valueOverride) {
      const id = st.openId;
      if (!id || !KINDS.includes(kind)) return false;
      if (kind === "memory") return saveMemory();
      const k = keyOf(id, kind);
      if (st.saving[k]) return false;
      const ta = $(`ta-detail-${kind}`);
      const text = valueOverride !== undefined ? valueOverride : ta ? ta.value : currentText(id, kind);
      const tooLong = personaProblem(text, st.limits);
      if (tooLong) {
        // (the server refuses it too: 400) say it here, before anything is sent
        showToast(tooLong);
        return false;
      }
      const typed = st.drafts[k]; // what the box held when Save was pressed (undefined: nothing typed over the saved text)
      st.saving[k] = true;
      render({ editors: true });
      try {
        const ticket = sent();
        const res = await api.put(`api/roster/${enc(id)}`, { [kind]: text });
        const merged = mergeSeatResponse(id, res, ticket);
        if ((!merged || typeof (res || {})[kind] !== "string") && (st.seatRev[id] || 0) <= ticket) {
          const cur = st.seats.get(id);
          if (cur) {
            st.seats.set(id, { ...cur, [kind]: text });
            st.seatRev[id] = ticket;
          }
        }
        // what was typed while the request was on its way is a LATER edit: it stays (an unsaved difference to the text saved now);
        // what was typed before it was sent is saved - or was replaced by the draft that was adopted
        if (st.drafts[k] === typed) delete st.drafts[k];
        dropRefine(k); // (a draft of the moderator's model, or one still being made, is about the text that was just replaced)
        showToast(`${KIND_LABELS[kind]}已保存`);
        return true;
      } catch (e) {
        showToast(e.detail || e.message);
        return false;
      } finally {
        delete st.saving[k];
        repaintEditors(id);
      }
    }

    // ---- what was typed while a save was on its way, and what a request that is on its way can still change ----------

    /** The typed text of the memory editor as it is now (a copy): taken when Save is pressed, to tell later typing from what was sent. */
    function snapshotMemDraft(id) {
      const d = st.memDraft[id];
      return d ? { imp: { ...(d.imp || {}) }, exp: d.exp } : null;
    }

    /**
     * After a save: what was typed BEFORE the request was sent is saved (or was replaced by the draft that was adopted) and goes;
     * what was typed while it was on its way is a later edit and stays - an unsaved difference to what is saved now.
     */
    function rebaseMemDraft(id, sentDraft) {
      const d = st.memDraft[id];
      if (!d || !sentDraft) return;
      if (d.imp) {
        for (const key of Object.keys(sentDraft.imp)) if (d.imp[key] === sentDraft.imp[key]) delete d.imp[key];
        if (!Object.keys(d.imp).length) delete d.imp;
      }
      if (sentDraft.exp !== undefined && d.exp === sentDraft.exp) delete d.exp;
      if (d.imp === undefined && d.exp === undefined) delete st.memDraft[id];
    }

    /** The generation of the refine of `k`: an answer that is on its way counts only while this is still the one it was started under. */
    const refineGenOf = (k) => `${st.refineEpoch}:${st.refineGen[k] || 0}`;

    /** Forget the draft (or the "busy" mark) of `k`, and make an answer that is still on its way for it stale. */
    function dropRefine(k) {
      st.refineGen[k] = (st.refineGen[k] || 0) + 1;
      delete st.refine[k];
    }

    /** Every refine of every seat (a new game, "clear all memory"). */
    function dropAllRefines() {
      st.refineEpoch++;
      st.refine = {};
    }

    /**
     * After a request that began in the editors: repaint them - unless the user is typing in one of them (a repaint would take the caret
     * away): the counters and the Save buttons follow without one.
     */
    function repaintEditors(id) {
      if (id !== st.openId) return; // (the panel shows another player by now - or none: it is drawn from the state when it is opened)
      if (hasFocus(el.editors)) {
        updatePersonaLive(id);
        updateMemoryLive(id);
        return;
      }
      render({ editors: true });
    }

    /**
     * Save the memory: only the impressions that changed and (when it changed) the lessons - `memory_parts`, the parts the
     * server merges.  `override` = the parts of an accepted draft ({impressions, experience, legacy?}).
     */
    async function saveMemory(override) {
      const id = st.openId;
      if (!id || id === MOD) return false;
      const k = keyOf(id, "memory");
      if (st.saving[k]) return false;
      const m = memoryState(id);
      let payload = override;
      if (!payload) {
        if (m.problem) {
          showToast(m.problem);
          return false;
        }
        payload = {};
        const changed = {};
        for (const o of m.others) {
          if (m.cleaned[o.id] !== cleanImpression(m.parts.impressions[o.id] || "")) changed[o.id] = m.cleaned[o.id];
        }
        if (Object.keys(changed).length) payload.impressions = changed;
        if (JSON.stringify(m.lessons) !== JSON.stringify(m.parts.experience)) payload.experience = m.lessons;
        if (!Object.keys(payload).length) return false; // nothing changed
      }
      const typed = snapshotMemDraft(id); // what was typed when Save was pressed
      st.saving[k] = true;
      render({ editors: true });
      try {
        const ticket = sent();
        const res = await api.put(`api/roster/${enc(id)}`, { memory_parts: payload });
        if (!mergeSeatResponse(id, res, ticket)) await load();
        rebaseMemDraft(id, typed); // (what was typed while the request was on its way stays)
        dropRefine(k);
        showToast(`${KIND_LABELS.memory}已保存`);
        return true;
      } catch (e) {
        showToast(e.detail || e.message); // (the server says which impression / which limit)
        return false;
      } finally {
        delete st.saving[k];
        repaintEditors(id);
      }
    }

    /**
     * "用主持人模型完善": the notes (the persona box, or the notes box of the memory) go to the moderator's model, the answer
     * is a DRAFT.  `fromLegacy`: tidy the old memory text instead (accepting the draft then deletes it).
     */
    async function refine(kind, how) {
      const id = st.openId;
      if (!id || !KINDS.includes(kind)) return;
      const k = keyOf(id, kind);
      if (st.refine[k] && st.refine[k].busy) return;
      const fromLegacy = Boolean(how && how.fromLegacy);
      let text;
      if (kind === "memory") {
        // (the notes are kept in step by the input events; the box itself is only the fallback)
        const box = $("ta-detail-memory-notes");
        const seat = seatOf(id);
        text = fromLegacy ? (seat && seat.parts ? seat.parts.legacy : "") : st.memNotes[id] || (box && typeof box.value === "string" ? box.value : "");
      } else {
        const ta = $(`ta-detail-${kind}`);
        text = ta ? ta.value : currentText(id, kind);
        st.drafts[k] = text;
      }
      if (!String(text).trim()) {
        showToast(`请先在「${KIND_LABELS[kind]}」里写几个要点，再让模型完善`);
        return;
      }
      // This request's generation.  What makes the text it was made from stop being the text (a save, a review that overwrote the memory, a
      // new game, "clear all memory") makes its answer stale - a draft or a failure alike - and a stale answer touches nothing: it must not
      // bring a draft back (accepting it would overwrite the memory that came after), nor wipe the state of a request that began later.
      st.refineGen[k] = (st.refineGen[k] || 0) + 1;
      const gen = refineGenOf(k);
      st.refine[k] = { busy: true, fromLegacy };
      render({ editors: true });
      let fresh = false;
      try {
        const res = await api.post(`api/roster/${enc(id)}/refine`, { kind, text });
        if (refineGenOf(k) !== gen) return;
        const draft = res && typeof res.draft === "string" ? res.draft : "";
        if (!draft.trim()) throw new Error("模型没有返回草稿，请稍后重试");
        // a memory draft comes with its structure ({impressions of every other seat, experience}: no legacy, no record)
        const parts =
          kind === "memory" && res.memory_parts && typeof res.memory_parts === "object"
            ? { impressions: normalizeParts(res.memory_parts, "").impressions, experience: normalizeParts(res.memory_parts, "").experience }
            : null;
        st.refine[k] = { draft, parts, fromLegacy };
        st.expanded[k] = true;
        fresh = true;
      } catch (e) {
        if (refineGenOf(k) === gen) {
          delete st.refine[k];
          showToast(`完善失败：${e.detail || e.message}`);
        }
      } finally {
        render({ editors: true });
        // the draft (and its "采用并保存" button) appears below the text box: bring it into view
        const node = fresh && typeof doc.querySelector === "function" ? doc.querySelector(".detail-draft") : null;
        if (node && typeof node.scrollIntoView === "function") node.scrollIntoView({ block: "nearest" });
      }
    }

    async function acceptDraft(kind) {
      const id = st.openId;
      if (!id) return;
      const k = keyOf(id, kind);
      const rf = st.refine[k];
      if (!rf || typeof rf.draft !== "string") return;
      const gen = refineGenOf(k);
      let ok;
      if (kind === "memory") {
        if (!rf.parts) {
          showToast("这份草稿没有带结构化的印象和经验，不能直接保存；请重新点「用主持人模型完善」");
          return;
        }
        // the draft replaces the impressions and the lessons; accepting a tidied-up legacy text also deletes that text
        const notes = st.memNotes[id];
        ok = await saveMemory({ impressions: rf.parts.impressions, experience: rf.parts.experience, ...(rf.fromLegacy ? { legacy: "" } : {}) });
        // (the notes are in the memory now: they must not be merged a second time - but notes typed while the save was on its way are new)
        if (ok && !rf.fromLegacy && st.memNotes[id] === notes) delete st.memNotes[id];
      } else {
        ok = await saveText(kind, rf.draft);
      }
      // a save that failed gives the draft back - unless something invalidated it meanwhile (a review, a new game) or a newer request began
      if (!ok && refineGenOf(k) === gen && !st.refine[k]) st.refine[k] = rf;
      repaintEditors(id);
    }

    function tidyLegacy() {
      return refine("memory", { fromLegacy: true });
    }

    /** "删除旧记忆": only that section goes (the record, impressions and lessons stay). */
    async function dropLegacy() {
      const id = st.openId;
      if (!id || id === MOD) return;
      const choice = await openDialog({
        title: "删除旧记忆",
        bodyHtml: `<div>将删除 <strong>${seatLabelOf(id)}</strong> 的「旧记忆（待整理）」。战绩、印象和经验不受影响。此操作不能撤销。</div>`,
        okText: "确认删除",
        danger: true,
      });
      if (choice.ok && st.openId === id) await saveMemory({ legacy: "" });
    }

    function discardDraft(kind) {
      const id = st.openId;
      if (!id) return;
      dropRefine(keyOf(id, kind));
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
        case "toggle-editor": {
          if (!st.openId || !KINDS.includes(kind)) break;
          const k = keyOf(st.openId, kind);
          // keep what was typed before the section is repainted (the persona box; the memory's inputs are kept by their events)
          if (kind === "persona") {
            const ta = $("ta-detail-persona");
            if (ta && typeof ta.value === "string") st.drafts[k] = ta.value;
          }
          st.expanded[k] = !st.expanded[k];
          render({ editors: true });
          break;
        }
        case "refine":
          refine(kind);
          break;
        case "tidy-legacy":
          tidyLegacy();
          break;
        case "drop-legacy":
          dropLegacy();
          break;
        case "toggle-changes":
          if (st.openId) {
            st.changesOpen[st.openId] = st.changesOpen[st.openId] === false;
            render({ live: true });
          }
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
          delete st.memDraft[st.openId]; // (what was typed over the old memory is moot: show what the file says now)
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
      const value = String(t.value === undefined || t.value === null ? "" : t.value);
      const key = t.getAttribute("data-draft-key");
      if (key) {
        // the persona box: its counter, the over-the-limit hint and the Save button follow the text without a repaint
        st.drafts[key] = value;
        const id = key.split(":")[0];
        updatePersonaLive(id);
        return;
      }
      const id = st.openId;
      if (!id) return;
      const impId = t.getAttribute("data-imp-id");
      if (impId) {
        // one impression of the memory
        const draft = st.memDraft[id] || (st.memDraft[id] = {});
        (draft.imp || (draft.imp = {}))[impId] = value;
        updateMemoryLive(id);
      } else if (t.id === "ta-detail-memory-exp") {
        (st.memDraft[id] || (st.memDraft[id] = {})).exp = value;
        updateMemoryLive(id);
      } else if (t.id === "ta-detail-memory-notes") {
        st.memNotes[id] = value;
      } else if (t.id === "input-avatar-code") {
        onCodeInput(id, value);
      } else if (t.id === "input-detail-name") {
        st.nameDraft[id] = value;
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
      } else if (typeof evt.target.getAttribute === "function" && evt.target.getAttribute("data-imp-id")) {
        // an impression is ONE line (the server folds line breaks into spaces): Enter does not start a second one
        if (typeof evt.preventDefault === "function") evt.preventDefault();
      }
    }

    // -----------------------------------------------------------------------
    // dialogs
    // -----------------------------------------------------------------------

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
          el.dialogOk.disabled = false;
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
      const idle = isIdle();
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
      if (el.btnClear) el.btnClear.disabled = lock;
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
        const ticket = sent();
        const res = await api.post("api/roster/avatars:regenerate");
        if (res && Array.isArray(res.seats) && res.seats.length) {
          applyRoster({ ...res, memory_enabled: st.memoryEnabled, editable: st.editable }, ticket);
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

    async function clearMemory() {
      const choice = await openDialog({
        title: "清除全部记忆",
        bodyHtml:
          `<div>将清空<strong>全部 12 名玩家</strong>的记忆文件（战绩、对其他玩家的印象、经验和待整理的旧记忆）。此操作不能撤销，建议先在「设置」里导出存档。</div>` +
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
        st.memDraft = {};
        st.memNotes = {};
        dropAllRefines(); // (a refine that is still on its way was made from text that is gone)
        await load();
        showToast(include ? "已清除全部记忆和性格设定" : "已清除全部记忆");
        onChange();
      } catch (e) {
        showToast(`清除失败：${e.message}`);
      }
    }

    async function setMemoryEnabled(enabled) {
      try {
        const ticket = sent();
        const res = await api.put("api/roster/memory-enabled", { enabled });
        if (ticket > st.metaRev) {
          // (an answer older than one that already came - a later read of the roster - must not roll the switch back)
          st.memoryEnabled = res && typeof res.enabled === "boolean" ? res.enabled : enabled;
          st.metaRev = ticket;
        }
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
      if (action === "clear") {
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
          st.memDraft = {};
          st.memNotes = {};
          dropAllRefines(); // (a refine that is still on its way belongs to the game before)
          st.nameDraft = {};
          st.nameError = {};
          st.avatarOpen = {};
          st.codeDraft = {};
          st.codeNote = {};
          st.changesOpen = {};
          load();
        }
      }
      renderControls();
    }

    /** review.progress finished for someone (`id`): their memory file was overwritten - what was typed over the old one is moot. */
    function onReviewDone(id) {
      if (id) {
        delete st.memDraft[id];
        delete st.memNotes[id];
        dropRefine(keyOf(id, "memory")); // (a refine that is still on its way was made from the memory that was just overwritten)
      }
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
      clearMemory,
      regenAll,
      _state: st,
    };
  }

  root.ArenaRosterUI = {
    create,
    DEFAULT_LIMITS,
    PERSONA_TEMPLATE,
    normalizeLimits,
    normalizeParts,
    textLength,
    personaText,
    personaLength,
    personaProblem,
    cleanImpression,
    parseExperience,
    memoryBodyText,
    memoryChars,
    memoryProblem,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = root.ArenaRosterUI;
})(typeof window !== "undefined" ? window : globalThis);
