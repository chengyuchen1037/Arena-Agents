/* ==========================================================================
   Arena-Agents v0.13 — what a person does at the seat they play: the input box, the floor, abilities,
   request answers (docs/v0.13-plan.md F11-F19, contract §11.7)
   Loaded after takeover.js and before app.js. Exposes window.ArenaComposerUI.create(ctx).

   The input box (#composer, under the feed) follows the selected channel; every channel keeps its own draft.
     Enter sends, Shift+Enter breaks the line; the counter "23/300" uses the server's `max_chars`.
     The send button says 发送 / 禁言中 (hover: why) / 已暂停 / 未开始 / 已结束 - the text box itself can always be
     typed in.  结束发言 only while the seat holds the floor of that channel; 技能 opens the list of the abilities
     that are enabled for the seat -> a form generated from the scenario's `params` -> a second confirmation.
   Request cards: a recipient seat answers by clicking an option (choice) or typing (text); an answered open
   request can be answered again.
   Every rule (whose turn, how long, how many) is the server's: this file sends the seat's words to

     POST api/seat/speak {channel_id, content, based_on_seq}   POST api/seat/pass {channel_id}
     POST api/seat/respond {request_id, value}                  POST api/seat/ability {ability_id, params}

   and shows the answer (a refusal keeps the draft; STALE asks to confirm and send again).  What is allowed
   comes from `session.takeover` (view.js composerModel / requestAction); nothing is derived here.

   ctx: { api, Core, View, showToast, getSession, getViewpoint, getSelectedChannel, getClockNow,
          getFeedNode(), openDialog(options), onChange() }
   ========================================================================== */

(function (root) {
  "use strict";

  // common parameter names -> a word the player understands (the scenario only names them)
  const PARAM_WORDS = {
    target: "目标",
    declaration: "宣言",
    reason: "理由",
    note: "备注",
    message: "留言",
    text: "文字",
    player: "玩家",
    players: "玩家",
    direction: "方向",
    side: "选项",
    count: "数量",
  };

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
    const getSelectedChannel = opts.getSelectedChannel || (() => null);
    const getClockNow = opts.getClockNow || (() => 0);
    const getFeedNode = opts.getFeedNode || (() => null);
    const openDialog = opts.openDialog || (async () => ({ ok: true, values: {} }));
    const onChange = opts.onChange || (() => {});
    const onSent = opts.onSent || (() => {});
    const $ = (id) => (typeof doc.getElementById === "function" ? doc.getElementById(id) : null);

    const el = {
      composer: $("composer"),
      notice: $("composer-notice"),
      input: $("composer-input"),
      count: $("composer-count"),
      hint: $("composer-hint"),
      btnSend: $("btn-composer-send"),
      btnPass: $("btn-composer-pass"),
      btnAbility: $("btn-composer-ability"),
    };

    const st = {
      drafts: new Map(), // "<session>|<seat>|<channel>" -> {text, seq}: seq = what the player had seen when the draft began
      scope: "", // "<session>|<seat>": the drafts of another game / seat are dropped
      key: null, // draft key of the channel the box shows
      sending: false,
      notice: "",
      noticeKey: null,
      reqDrafts: new Map(), // request id -> text typed into a text request's input
      busy: new Set(), // request ids with a request in flight
      keyboard: false,
      grownFor: null, // the text the box was last sized for
    };

    const session = () => getSession() || {};

    // -----------------------------------------------------------------------
    // small helpers
    // -----------------------------------------------------------------------

    function on(node, type, handler) {
      if (node && typeof node.addEventListener === "function") node.addEventListener(type, handler);
    }

    function closestFrom(target, selector) {
      let node = target;
      if (node && typeof node.closest !== "function" && node.parentElement) node = node.parentElement;
      return node && typeof node.closest === "function" ? node.closest(selector) : null;
    }

    function setText(node, text) {
      if (node && node.textContent !== text) node.textContent = text;
    }

    function setTitle(node, text) {
      if (node && typeof node.setAttribute === "function") node.setAttribute("title", text || "");
    }

    function toggleClass(node, name, on) {
      if (node && node.classList && typeof node.classList.toggle === "function") node.classList.toggle(name, Boolean(on));
    }

    /** What the player has seen of the game: the ledger position of the server's last seat state. */
    function currentSeq() {
      const s = session();
      const t = s.takeover;
      return (t && t.seq) || s.seq || 0;
    }

    function played() {
      const s = session();
      const seat = View.takeoverSeat(s);
      return seat && getViewpoint() === seat ? seat : null;
    }

    function draftOf(key) {
      let d = st.drafts.get(key);
      if (!d) {
        d = { text: "", seq: 0 };
        st.drafts.set(key, d);
      }
      return d;
    }

    /** Drafts belong to one (game, seat): another game or another seat starts with empty boxes. */
    function ensureScope(s, seat) {
      const scope = `${s.session_id || ""}|${seat}`;
      if (scope === st.scope) return;
      st.scope = scope;
      st.drafts.clear();
      st.reqDrafts.clear();
      st.key = null;
      st.notice = "";
    }

    function currentModel() {
      const channelId = getSelectedChannel();
      const text = el.input ? el.input.value : "";
      return View.composerModel(session(), channelId, text, { clockNow: getClockNow() });
    }

    // -----------------------------------------------------------------------
    // rendering (the markup is static: only texts / states change, so typing never loses its focus)
    // -----------------------------------------------------------------------

    function render() {
      if (!el.composer) return;
      const s = session();
      const seat = played();
      const channelId = getSelectedChannel();
      if (!seat || !channelId) {
        el.composer.hidden = true;
        st.key = null;
        return;
      }
      ensureScope(s, seat);
      const key = `${st.scope}|${channelId}`;
      if (st.key !== key) {
        saveCurrent();
        st.key = key;
        if (st.noticeKey !== key) st.notice = "";
        if (el.input) el.input.value = draftOf(key).text;
      }
      const model = View.composerModel(s, channelId, el.input ? el.input.value : "", { clockNow: getClockNow() });
      el.composer.hidden = false;
      autoGrow();
      toggleClass(el.composer, `is-${model.mode}`, true);
      for (const mode of ["send", "muted", "paused", "idle", "finished"]) {
        if (mode !== model.mode) toggleClass(el.composer, `is-${mode}`, false);
      }

      if (el.input && typeof el.input.setAttribute === "function") {
        const title = View.channelTitle(s, seat, channelId);
        // a touch screen has no Shift+Enter: the short form keeps the box to one line on a phone
        const page = doc.documentElement;
        const touch = Boolean(page && typeof page.getAttribute === "function" && page.getAttribute("data-input") === "touch");
        el.input.setAttribute("placeholder", touch ? `在「${title}」发言` : `在「${title}」发言（Enter 发送，Shift+Enter 换行）`);
      }
      setText(el.btnSend, st.sending ? "发送中…" : model.label);
      if (el.btnSend) {
        el.btnSend.disabled = !model.sendEnabled || st.sending;
        setTitle(el.btnSend, model.reason);
      }
      setText(el.count, model.max ? `${model.count}/${model.max}` : `${model.count} 字`);
      toggleClass(el.count, "over", model.over > 0);
      setTitle(el.count, model.over > 0 ? `超出 ${model.over} 字：发送时末尾会被截掉` : "字数（以发送时的上限为准）");
      setText(el.hint, model.hint);
      if (el.btnPass) {
        el.btnPass.hidden = !model.showPass;
        el.btnPass.disabled = !model.canPass || st.sending;
        setTitle(el.btnPass, model.passTitle);
      }
      if (el.btnAbility) {
        el.btnAbility.disabled = !model.abilityEnabled;
        setTitle(el.btnAbility, model.abilityReason);
      }
      if (el.notice) {
        const text = st.noticeKey === key ? st.notice : "";
        setText(el.notice, text);
        el.notice.hidden = !text;
      }
    }

    function saveCurrent() {
      if (st.key && el.input) draftOf(st.key).text = el.input.value;
    }

    /**
     * The box grows with what is typed (up to the CSS max-height, then it scrolls): one line on a phone, two on a
     * desktop until the text needs more.  Only when the text changed: reading the height forces a layout.
     */
    function autoGrow() {
      const box = el.input;
      if (!box || !box.style) return;
      if (box.value === st.grownFor) return;
      st.grownFor = box.value;
      box.style.height = "auto";
      const needed = Number(box.scrollHeight) || 0;
      if (needed > 0) box.style.height = `${needed + 2}px`; // (+ the two borders)
    }

    function showNotice(text) {
      st.notice = text;
      st.noticeKey = st.key;
      render();
    }

    // -----------------------------------------------------------------------
    // speak / pass
    // -----------------------------------------------------------------------

    /** POST a seat action; the server's answer, or null when the request itself failed (a toast says why). */
    async function act(path, body) {
      try {
        return await api.post(path, body);
      } catch (err) {
        showToast(err.message);
        return null;
      }
    }

    async function send() {
      if (st.sending || !st.key) return false;
      const model = currentModel();
      if (!model.visible) return false;
      if (model.mode !== "send") {
        showNotice(model.reason);
        return false;
      }
      if (!model.sendEnabled) return false;
      const draft = draftOf(st.key);
      draft.text = el.input.value;
      const content = draft.text.trim();
      const key = st.key;
      st.sending = true;
      render();
      let res = null;
      try {
        res = await act("api/seat/speak", {
          channel_id: model.channelId,
          content,
          based_on_seq: draft.seq || currentSeq(),
        });
      } finally {
        st.sending = false;
      }
      if (res && res.accepted) {
        const d = draftOf(key);
        d.text = "";
        d.seq = 0;
        if (st.key === key && el.input) el.input.value = "";
        st.notice = "";
        if (res.truncated && res.truncated.dropped_chars) {
          showToast(`发言超出字数上限，末尾 ${res.truncated.dropped_chars} 个字被截掉了`);
        }
        onSent();
      } else if (res) {
        // refused: the draft stays.  STALE: the world changed while it was being written - read on, then send again
        if (res.code === "STALE") {
          draftOf(key).seq = currentSeq();
          showNotice("局面刚刚发生了重要变化（例如阶段或身份变更），草稿已保留。请确认内容后，再点一次「发送」。");
        } else {
          showNotice(res.message || "发送失败");
        }
      }
      render();
      onChange();
      return Boolean(res && res.accepted);
    }

    async function pass() {
      const model = currentModel();
      if (!model.visible || !model.showPass) return false;
      if (!model.canPass) {
        showToast(model.passTitle || "现在不能结束发言");
        return false;
      }
      st.sending = true;
      render();
      let res = null;
      try {
        res = await act("api/seat/pass", { channel_id: model.channelId });
      } finally {
        st.sending = false;
      }
      if (res && res.accepted) showToast("已结束发言");
      else if (res) showNotice(res.message || "结束发言失败");
      render();
      onChange();
      return Boolean(res && res.accepted);
    }

    // -----------------------------------------------------------------------
    // request cards
    // -----------------------------------------------------------------------

    function findRequest(requestId) {
      return (session().requests || []).find((r) => r.id === requestId) || null;
    }

    /**
     * The answer part of a request card (empty for a seat that is not a recipient): a choice shows its
     * options (the current one marked), a text request an input.  The text typed so far is NOT part of the
     * markup (typing must not make the card look different): `restoreRequestDrafts` puts it back.
     */
    function requestActionHtml(card) {
      const a = card && card.action;
      if (!a) return "";
      const id = esc(card.id);
      const disabled = a.enabled ? "" : " disabled";
      const mine = a.answered
        ? `<div class="req-mine">你的${card.kind === "text" ? "回答" : "选择"}：<strong>${esc(
            a.myAnswer.label
          )}</strong><span class="req-mine-hint">（请求关闭前可以修改）</span></div>`
        : `<div class="req-mine pending">轮到你作答了</div>`;
      let body;
      if (card.kind === "text") {
        const limit = a.maxChars ? `（最多 ${a.maxChars} 字）` : "";
        body =
          `<textarea class="req-text" rows="2" data-req-text="${id}" data-draft-key="req:${id}"` +
          ` data-initial="${esc(a.myAnswer ? a.myAnswer.value : "")}" aria-label="你的回答"` +
          ` placeholder="写下你的回答${limit}"${disabled}></textarea>` +
          `<div class="req-text-row"><button type="button" class="btn btn-xs btn-primary-outline req-submit"` +
          ` data-req-submit="${id}"${disabled}>${a.answered ? "修改回答" : "提交"}</button></div>`;
      } else {
        body =
          `<div class="req-options" role="group" aria-label="选项">` +
          a.options
            .map(
              (o) =>
                `<button type="button" class="req-opt${o.selected ? " selected" : ""}" data-req-answer="${id}"` +
                ` data-value="${esc(o.id)}" aria-pressed="${o.selected ? "true" : "false"}"${disabled}>` +
                `${o.selected ? "✓ " : ""}${esc(o.label)}</button>`
            )
            .join("") +
          `</div>`;
      }
      const why = a.enabled ? "" : `<div class="req-action-hint">${esc(a.disabledReason)}</div>`;
      return `<div class="req-action" data-req-action="${id}">${mine}${body}${why}</div>`;
    }

    /** Put the typed text back into the text-request inputs of freshly drawn cards. */
    function restoreRequestDrafts(container) {
      if (!container || typeof container.querySelectorAll !== "function") return;
      for (const box of Array.from(container.querySelectorAll("textarea[data-req-text]"))) {
        const id = box.getAttribute("data-req-text");
        box.value = st.reqDrafts.has(id) ? st.reqDrafts.get(id) : box.getAttribute("data-initial") || "";
      }
    }

    async function respond(requestId, value) {
      const s = session();
      const seat = played();
      const request = findRequest(requestId);
      const action = seat && request ? View.requestAction(s, seat, request) : null;
      if (!action) {
        showToast("这个请求现在不能作答");
        return false;
      }
      if (!action.enabled) {
        showToast(action.disabledReason);
        return false;
      }
      if (st.busy.has(requestId)) return false;
      st.busy.add(requestId);
      let res = null;
      try {
        res = await act("api/seat/respond", { request_id: requestId, value: String(value) });
      } finally {
        st.busy.delete(requestId);
      }
      if (res && res.accepted) {
        st.reqDrafts.delete(requestId);
        const option = action.options.find((o) => String(o.id) === String(value));
        showToast(`已提交：${option ? option.label : String(value)}`);
      } else if (res) {
        showToast(res.message || "提交失败");
      }
      onChange();
      return Boolean(res && res.accepted);
    }

    function submitText(requestId) {
      const text = String(st.reqDrafts.get(requestId) || "").trim();
      if (!text) {
        showToast("先写下你的回答");
        return Promise.resolve(false);
      }
      return respond(requestId, text);
    }

    // -----------------------------------------------------------------------
    // abilities: list -> form (from the scenario's params) -> a second confirmation
    // -----------------------------------------------------------------------

    function paramWord(name) {
      return PARAM_WORDS[name] || name;
    }

    function playerLabel(s, seat, id) {
      const p = View.participantOf(s, id);
      const base = p ? `${p.seat} 号 ${p.name}` : String(id);
      return id === seat ? `${base}（我）` : base;
    }

    const paramId = (abilityId, name) => `ability-param-${abilityId}-${name}`;

    function paramFieldHtml(s, seat, ability, p) {
      const id = esc(paramId(ability.id, p.name));
      const label = `${esc(paramWord(p.name))}${p.optional ? "（可选）" : ""}`;
      let input;
      if (p.type === "player_id") {
        const options = (p.candidates || [])
          .map((cid) => `<option value="${esc(cid)}">${esc(playerLabel(s, seat, cid))}</option>`)
          .join("");
        input = `<select class="form-input" id="${id}"><option value="">${p.optional ? "（不选）" : "请选择一名玩家"}</option>${options}</select>`;
      } else if (p.type === "player_ids") {
        input =
          `<div class="ability-checks" id="${id}">` +
          (p.candidates || [])
            .map(
              (cid) =>
                `<label class="checkbox-label"><input type="checkbox" id="${id}-${esc(cid)}" value="${esc(cid)}" />` +
                `<span>${esc(playerLabel(s, seat, cid))}</span></label>`
            )
            .join("") +
          `</div>`;
      } else if (p.type === "enum") {
        const options = (p.values || []).map((v) => `<option value="${esc(v)}">${esc(v)}</option>`).join("");
        input = `<select class="form-input" id="${id}"><option value="">${p.optional ? "（不选）" : "请选择"}</option>${options}</select>`;
      } else if (p.type === "int") {
        const range = p.min !== null && p.min !== undefined && p.max !== null && p.max !== undefined ? `（${p.min}–${p.max}）` : "";
        input = `<input type="number" class="form-input" id="${id}" step="1"${
          p.min !== null && p.min !== undefined ? ` min="${esc(p.min)}"` : ""
        }${p.max !== null && p.max !== undefined ? ` max="${esc(p.max)}"` : ""} placeholder="整数${range}" />`;
      } else {
        const limit = p.max_chars ? `（最多 ${p.max_chars} 字）` : "";
        input = `<textarea class="form-input" id="${id}" rows="2" ${
          p.max_chars ? `maxlength="${esc(p.max_chars)}" ` : ""
        }placeholder="${esc(paramWord(p.name))}${limit}"></textarea>`;
      }
      return `<div class="form-group ability-field"><label class="form-label" for="${id}">${label}</label>${input}</div>`;
    }

    function abilityDialogBody(s, seat, abilities) {
      return abilities
        .map((ability, i) => {
          const pickId = esc(`ability-pick-${ability.id}`);
          const left = ability.quota_left > 1 ? `　还可使用 ${ability.quota_left} 次` : "";
          const fields = ability.params.map((p) => paramFieldHtml(s, seat, ability, p)).join("");
          return (
            `<div class="ability-card">` +
            `<label class="ability-pick"><input type="radio" name="ability-pick" id="${pickId}" value="${esc(ability.id)}"` +
            ` data-ability-pick="${esc(ability.id)}"${i === 0 ? " checked" : ""} />` +
            `<span class="ability-name">${esc(ability.id)}</span><span class="ability-left">${esc(left)}</span></label>` +
            `<div class="ability-desc">${esc(ability.description || "（没有说明）")}</div>` +
            `<div class="ability-form" id="${esc(`ability-form-${ability.id}`)}"${i === 0 ? "" : " hidden"}>${fields}</div>` +
            `</div>`
          );
        })
        .join("");
    }

    /** Read the form of the picked ability: {ability, params, errors}. */
    function collectAbility(abilities) {
      const checked = abilities.filter((a) => {
        const node = $(`ability-pick-${a.id}`);
        return node && node.checked;
      });
      const ability = checked[0] || abilities[0];
      const params = {};
      const errors = [];
      for (const p of ability.params) {
        let value;
        if (p.type === "player_ids") {
          value = (p.candidates || []).filter((cid) => {
            const box = $(`${paramId(ability.id, p.name)}-${cid}`);
            return Boolean(box && box.checked);
          });
          if (!value.length) value = undefined;
        } else {
          const node = $(paramId(ability.id, p.name));
          const raw = node ? String(node.value === undefined || node.value === null ? "" : node.value).trim() : "";
          if (raw) value = p.type === "int" ? Number(raw) : raw;
        }
        if (value === undefined) {
          if (!p.optional) errors.push(`请填写「${paramWord(p.name)}」`);
          continue;
        }
        if (p.type === "int" && !Number.isInteger(value)) {
          errors.push(`「${paramWord(p.name)}」要填整数`);
          continue;
        }
        params[p.name] = value;
      }
      return { ability, params, errors };
    }

    function paramSummaryHtml(s, seat, ability, params) {
      const rows = ability.params
        .filter((p) => params[p.name] !== undefined)
        .map((p) => {
          const v = params[p.name];
          const shown =
            p.type === "player_id"
              ? playerLabel(s, seat, v)
              : p.type === "player_ids"
                ? v.map((id) => playerLabel(s, seat, id)).join("、")
                : String(v);
          return `<li><span class="ability-k">${esc(paramWord(p.name))}</span><span class="ability-v">${esc(shown)}</span></li>`;
        })
        .join("");
      return rows ? `<ul class="ability-summary">${rows}</ul>` : "";
    }

    /** Choose an ability and fill its form (the dialog opens again with an error toast until the form is valid). */
    async function chooseAbility(abilities) {
      const s = session();
      const seat = View.takeoverSeat(s);
      const body = abilityDialogBody(s, seat, abilities);
      const pickHandler = (evt) => {
        const node = closestFrom(evt && evt.target, "[data-ability-pick]");
        const picked = node && typeof node.getAttribute === "function" ? node.getAttribute("data-ability-pick") : null;
        if (!picked) return;
        for (const a of abilities) {
          const form = $(`ability-form-${a.id}`);
          if (form) form.hidden = a.id !== picked;
        }
      };
      const dialogBody = $("dialog-body");
      on(dialogBody, "change", pickHandler);
      try {
        for (;;) {
          const res = await openDialog({
            title: "使用技能",
            bodyHtml: body,
            okText: "下一步",
            cancelText: "取消",
            collect: () => collectAbility(abilities),
          });
          if (!res.ok) return null;
          const got = res.values;
          if (got.errors.length) {
            showToast(got.errors[0]);
            continue;
          }
          return got;
        }
      } finally {
        if (dialogBody && typeof dialogBody.removeEventListener === "function") {
          dialogBody.removeEventListener("change", pickHandler);
        }
      }
    }

    async function useAbility() {
      const s = session();
      const seat = played();
      if (!seat) return false;
      const model = currentModel();
      if (!model.visible || !model.abilityEnabled) {
        showToast(model.abilityReason || "现在没有可用的技能");
        return false;
      }
      const abilities = s.takeover.abilities;
      const picked = await chooseAbility(abilities);
      if (!picked) return false;
      const { ability, params } = picked;
      const sure = await openDialog({
        title: `确认使用技能「${ability.id}」？`,
        bodyHtml:
          `<p class="dialog-p">${esc(ability.description || "")}</p>` +
          paramSummaryHtml(session(), seat, ability, params) +
          `<p class="dialog-p dialog-warn">技能释放后不能撤销${
            ability.quota_left > 1
              ? `（用完这次后还剩 ${ability.quota_left - 1} 次）`
              : "（这是它的最后一次使用机会，用完不再恢复）"
          }，会公开发生在对局里。</p>`,
        okText: "确认使用",
        cancelText: "返回",
        danger: true,
      });
      if (!sure.ok) return false;
      if (session().status !== "running") {
        showToast(`${View.notRunningText(session().status)}，不能使用技能`);
        return false;
      }
      const res = await act("api/seat/ability", { ability_id: ability.id, params });
      if (res && res.accepted) showToast(`已使用技能「${ability.id}」`);
      else if (res) showToast(res.message || "使用技能失败");
      onChange();
      return Boolean(res && res.accepted);
    }

    // -----------------------------------------------------------------------
    // soft keyboard (phones): the page follows the visible part of the screen
    // -----------------------------------------------------------------------

    /** `innerHeight` is the page, `visualHeight` what the keyboard leaves: more than ~120 px less = it is open. */
    function keyboardOpen(innerHeight, visualHeight) {
      return Number(innerHeight) - Number(visualHeight) > 120;
    }

    function bindViewport() {
      const vv = win.visualViewport;
      if (!vv || typeof vv.addEventListener !== "function") return;
      const html = doc.documentElement;
      const update = () => {
        const open = keyboardOpen(win.innerHeight, vv.height);
        if (open !== st.keyboard) {
          st.keyboard = open;
          toggleClass(html, "kb-open", open);
        }
        if (html && html.style && typeof html.style.setProperty === "function") {
          html.style.setProperty("--vvh", `${Math.round(Number(vv.height) || 0)}px`);
        }
        if (open && typeof win.scrollTo === "function") win.scrollTo(0, 0);
      };
      vv.addEventListener("resize", update);
      vv.addEventListener("scroll", update);
    }

    // -----------------------------------------------------------------------
    // events
    // -----------------------------------------------------------------------

    on(el.input, "input", () => {
      if (!st.key) return;
      const d = draftOf(st.key);
      d.text = el.input.value;
      // the draft starts here: it is based on what the player has seen right now
      if (!d.text.trim()) d.seq = 0;
      else if (!d.seq) d.seq = currentSeq();
      if (st.notice) st.notice = "";
      render();
    });
    on(el.input, "keydown", (evt) => {
      if (!evt || evt.key !== "Enter" || evt.shiftKey) return;
      if (evt.isComposing || evt.keyCode === 229) return; // an IME is confirming its candidate
      if (typeof evt.preventDefault === "function") evt.preventDefault();
      send();
    });
    on(el.btnSend, "click", () => {
      send();
    });
    on(el.btnPass, "click", () => {
      pass();
    });
    on(el.btnAbility, "click", () => {
      useAbility();
    });

    // the answers of request cards (the cards are redrawn by app.js: one listener on their container)
    const feed = getFeedNode();
    on(feed, "click", (evt) => {
      const opt = closestFrom(evt && evt.target, "[data-req-answer]");
      if (opt) {
        respond(opt.getAttribute("data-req-answer"), opt.getAttribute("data-value"));
        return;
      }
      const sub = closestFrom(evt && evt.target, "[data-req-submit]");
      if (sub) submitText(sub.getAttribute("data-req-submit"));
    });
    on(feed, "input", (evt) => {
      const box = closestFrom(evt && evt.target, "[data-req-text]");
      if (!box) return;
      const id = box.getAttribute("data-req-text");
      const value = box.value !== undefined ? box.value : evt.target && evt.target.value;
      st.reqDrafts.set(id, String(value === undefined || value === null ? "" : value));
    });
    bindViewport();

    return {
      render,
      send,
      pass,
      respond,
      submitText,
      useAbility,
      requestActionHtml,
      restoreRequestDrafts,
      keyboardOpen,
      draft: (channelId) => {
        const d = st.drafts.get(`${st.scope}|${channelId}`);
        return d ? { ...d } : { text: "", seq: 0 };
      },
      _state: st,
    };
  }

  root.ArenaComposerUI = { create };
})(typeof window !== "undefined" ? window : this);
