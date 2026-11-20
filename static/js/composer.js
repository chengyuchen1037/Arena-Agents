/* ==========================================================================
   Arena-Agents v0.13 — what a person does at the seat they play: the input box, the floor, abilities,
   request answers (docs/v0.13-plan.md F11-F19, contract §11.7)
   Loaded after takeover.js and before app.js. Exposes window.ArenaComposerUI.create(ctx).

   The input box (#composer, under the feed) follows the selected channel; every channel keeps its own draft.
     Enter sends, Shift+Enter breaks the line; the counter "23/300" uses the server's `max_chars` and counts live (v0.14, H16): over the
     limit the counter turns red and the send button is off.  A speech that reaches the server too long is refused whole (`TOO_LONG`, a
     receipt with `data: {chars, limit}`): nothing is sent, nothing is cut, the draft stays in the box and the server's sentence is shown.
     The send button says 发送 / 禁言中 (hover: why) / 已暂停 / 未开始 / 已结束 - the text box itself can always be
     typed in.  结束发言 only while the seat holds the floor of that channel; 技能 opens the list of the abilities
     that are enabled for the seat -> a form generated from the scenario's `params` -> a second confirmation.
   Request cards: a recipient seat answers by picking an option (choice) and pressing 「提交」, or by typing
   (text) and pressing 「提交」; an answered open request can be answered again (「修改回答」).  v0.13.1 (G45): a click
   on an option only PICKS it - nothing is sent until the card's own button is pressed, so a stray click can
   never cast a vote.  The pick lives on the page only (per request) and is dropped when the request closes;
   when the deadline comes the engine's default answer counts, never the pick.
   An answer on its way is a token (st.ops: one per game + seat + request): it greys the card's button (「提交中…」),
   and when it comes back it drops only the pick / text it SENT - what was picked or typed after the press is a newer
   wish and stays (the card then offers it as the change), a refusal changes nothing, and an answer that comes back
   after the game on screen changed (request ids repeat: "r1" again) touches nothing of the new page.
   Every rule (whose turn, how long, how many) is the server's: this file sends the seat's words to

     POST api/seat/speak {channel_id, content, based_on_seq}   POST api/seat/pass {channel_id}
     POST api/seat/respond {request_id, value}                  POST api/seat/ability {ability_id, params}

   and shows the answer (a refusal keeps the draft; STALE asks to confirm and send again; TOO_LONG says how much to shorten).  What is allowed
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

  // v0.13.1 (G45): this many game seconds before a request's deadline a pick that was not submitted is flagged
  const PICK_WARN_SECONDS = 15;
  // ... and the card's button while the answer it sent has not come back (one answer at a time per request)
  const SENDING_LABEL = "提交中…";
  const SENDING_TITLE = "上一次提交还没有完成，完成后可以再提交新的选择";

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
      picked: new Map(), // request id -> the option id picked on a choice card and not submitted yet (G45); belongs to `scope`
      ops: new Map(), // "<game>|<seat>|<request id>" -> {n, value}: the answer of that request on its way (G45, see respond)
      opSeq: 0, // the generation counter of those answers (`n` of the latest one)
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

    /** "<game>|<seat>": what drafts, picks and the answers on their way belong to (one game, one seat). */
    const scopeOf = (s, seat) => `${s.session_id || ""}|${seat}`;

    /** The scope that is on screen right now ("" while nobody is played). */
    function liveScope() {
      const s = session();
      const seat = View.takeoverSeat(s);
      return seat ? scopeOf(s, seat) : "";
    }

    /**
     * The answer of a request of the game and seat on screen that has not come back yet ({n, value}), else null.
     * (An answer of another game is on its way too, perhaps - under a key of its own: it is not this card's.)
     */
    function answerOnItsWay(requestId) {
      const scope = liveScope();
      return scope ? st.ops.get(`${scope}|${requestId}`) || null : null;
    }

    /** Drafts belong to one (game, seat): another game or another seat starts with empty boxes. */
    function ensureScope(s, seat) {
      const scope = scopeOf(s, seat);
      if (scope === st.scope) return;
      st.scope = scope;
      st.drafts.clear();
      st.reqDrafts.clear();
      st.picked.clear();
      st.key = null;
      st.notice = "";
    }

    /**
     * Make the page's drafts and picks those of the game and seat on screen NOW.  `render()` does it with the next paint,
     * but a click on a card, the restore of a typed text or the markup of a request card can come in between (the feed is
     * drawn before the box): what was picked or typed in the game before must not be read as this game's "r1".
     */
    function syncScope() {
      const s = session();
      const seat = View.takeoverSeat(s);
      if (seat) ensureScope(s, seat);
    }

    /** A pick belongs to an open request of this seat: once the request closes (or is gone) the pick goes too. */
    function prunePicks() {
      if (!st.picked.size) return;
      for (const id of Array.from(st.picked.keys())) {
        const request = findRequest(id);
        if (!request || request.status !== "open") st.picked.delete(id);
      }
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
      prunePicks();
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
      // (H16) the live count against the limit of the channel; over it the counter turns red and the send button is off - a speech that is
      // too long is sent back whole (TOO_LONG), never cut, so it has to be shortened first
      setText(el.count, model.max ? `${model.count}/${model.max}` : `${model.count} 字`);
      toggleClass(el.count, "over", model.over > 0);
      toggleClass(el.composer, "is-over", model.over > 0);
      setTitle(el.count, model.over > 0 ? model.overText : "字数 / 这个频道单条发言的上限（超过上限不能发送，需要先删减）");
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

    /** The sentence for a `TOO_LONG` refusal whose own `message` is missing: `data` = {chars, limit} of the receipt. */
    function tooLongText(data) {
      const d = data || {};
      return Number.isFinite(Number(d.chars)) && Number.isFinite(Number(d.limit))
        ? `这条发言 ${d.chars} 个字，超过了单条上限 ${d.limit} 字，没有发出，发言权还在。请删减后重发。`
        : "这条发言太长，没有发出，发言权还在。请删减后重发。";
    }

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
      if (model.over > 0) {
        showNotice(model.overText); // (H16) Enter over the limit: nothing is sent - the sentence says how much to shorten
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
        onSent();
      } else if (res) {
        // refused: the draft stays.  STALE: the world changed while it was being written - read on, then send again.
        // TOO_LONG (H16): nothing was sent and nothing was cut - the floor is still yours; the server's sentence says how long it was and the limit
        if (res.code === "STALE") {
          draftOf(key).seq = currentSeq();
          showNotice("局面刚刚发生了重要变化（例如阶段或身份变更），草稿已保留。请确认内容后，再点一次「发送」。");
        } else if (res.code === "TOO_LONG") {
          showNotice(res.message || tooLongText(res.data));
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
      syncScope(); // (the feed is drawn before the box: another game's pick must not show on this game's card)
      const id = esc(card.id);
      const disabled = a.enabled ? "" : " disabled";
      // the answer of this card that has not come back yet: the button is grey meanwhile, the options and the text box
      // stay usable (what is picked / typed now is a newer wish that survives that answer, see respond)
      const flight = answerOnItsWay(card.id);
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
          ` data-req-submit="${id}"${flight ? ` title="${esc(SENDING_TITLE)}"` : ""}${a.enabled && !flight ? "" : " disabled"}>` +
          `${flight ? SENDING_LABEL : a.answered ? "修改回答" : "提交"}</button></div>`;
      } else {
        // G45: a click only picks; the pick is sent by the card's own 「提交」 button
        const pick = View.requestPick(a, st.picked.get(card.id));
        const pressedId = pick.picked !== null ? pick.picked : null;
        let note = "";
        // (a pick that is the very answer being sent is not "unsubmitted": the button says 提交中…)
        if ((pick.kind === "new" || pick.kind === "change") && !(flight && flight.value === pick.picked)) {
          // (the 1 Hz countdown tick adds `urgent` to the note when the deadline is closer than PICK_WARN_SECONDS)
          const warnAt =
            card.deadlineClock !== null && card.deadlineClock !== undefined
              ? ` data-warn-at="${esc(String(Number(card.deadlineClock) - PICK_WARN_SECONDS))}"`
              : "";
          const text =
            pick.kind === "new"
              ? `已选：<strong>${esc(pick.pickedLabel)}</strong> · 未提交`
              : `已提交：${esc(pick.currentLabel)} · 改选：<strong>${esc(pick.pickedLabel)}</strong>（未提交）`;
          note =
            `<div class="req-pick" data-req-pick="${id}"${warnAt}>${text}` +
            `<span class="req-pick-warn">⚠ 快到时间了，点「${esc(pick.submitLabel)}」才算数；到点按默认答案处理</span></div>`;
        }
        const canSubmit = pick.canSubmit && !flight;
        const submitTitle = !a.enabled ? a.disabledReason : flight ? SENDING_TITLE : pick.canSubmit ? "" : "先选一个选项，再提交";
        body =
          `<div class="req-options" role="group" aria-label="选项">` +
          a.options
            .map((o) => {
              const isPicked = pressedId !== null && String(o.id) === pressedId;
              const pressed = pressedId !== null ? isPicked : o.selected;
              const cls = `req-opt${o.selected ? " selected" : ""}${isPicked && pick.dirty ? " picked" : ""}`;
              return (
                `<button type="button" class="${cls}" data-req-answer="${id}"` +
                ` data-value="${esc(o.id)}" aria-pressed="${pressed ? "true" : "false"}"${disabled}>` +
                `${o.selected ? "✓ " : ""}${esc(o.label)}</button>`
              );
            })
            .join("") +
          `</div>` +
          note +
          `<div class="req-text-row"><button type="button" class="btn btn-xs btn-primary-outline req-submit"` +
          ` data-req-confirm="${id}" title="${esc(submitTitle)}"${canSubmit ? "" : " disabled"}>` +
          `${flight ? SENDING_LABEL : esc(pick.submitLabel)}</button></div>`;
      }
      const why = a.enabled ? "" : `<div class="req-action-hint">${esc(a.disabledReason)}</div>`;
      return `<div class="req-action" data-req-action="${id}">${mine}${body}${why}</div>`;
    }

    /** Put the typed text back into the text-request inputs of freshly drawn cards. */
    function restoreRequestDrafts(container) {
      if (!container || typeof container.querySelectorAll !== "function") return;
      syncScope(); // (a text typed in the game before is not this game's draft of the same request id)
      for (const box of Array.from(container.querySelectorAll("textarea[data-req-text]"))) {
        const id = box.getAttribute("data-req-text");
        box.value = st.reqDrafts.has(id) ? st.reqDrafts.get(id) : box.getAttribute("data-initial") || "";
      }
    }

    /**
     * Send the answer of a request: a choice's picked option, a text request's text (G45).
     *
     * One answer at a time per request of the game and seat on screen: while it is on its way a second press sends
     * nothing and the card's button says 「提交中…」; what is picked or typed meanwhile is a newer wish of its own.  Every
     * answer is a token in `st.ops` ({n, value}), keyed by game + seat + request id - the same id of another game is
     * another slot ("r1" comes back in every game).  When the answer comes back
     *   - it releases only its own slot (a slot that holds another token is left alone),
     *   - it touches the page only while the game and seat it was sent for are still on screen: what the page holds under
     *     that request id now belongs to the game that is there now,
     *   - and then it drops only what it SENT: the pick / the text, if that is still what was sent.  A pick or a text
     *     made after the press stays (the card then offers it as the change); a refusal - or a request that failed -
     *     changes nothing: the pick stays and the person may press again.
     */
    async function respond(requestId, value) {
      syncScope();
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
      const scope = scopeOf(s, seat);
      const slot = `${scope}|${requestId}`;
      if (st.ops.has(slot)) return false; // a double press (or another tab's click): one answer at a time
      const sent = String(value);
      const op = { n: ++st.opSeq, value: sent };
      st.ops.set(slot, op);
      let res = null;
      try {
        onChange(); // the card greys its button while the answer is on its way
        res = await act("api/seat/respond", { request_id: requestId, value: sent });
      } finally {
        // (every way out of here frees the slot - but only the slot this answer took)
        if (st.ops.get(slot) === op) st.ops.delete(slot);
      }
      const accepted = Boolean(res && res.accepted);
      if (liveScope() === scope) {
        if (accepted) {
          if (st.picked.get(requestId) === sent) st.picked.delete(requestId);
          const typed = st.reqDrafts.get(requestId);
          if (typed !== undefined && String(typed).trim() === sent) st.reqDrafts.delete(requestId);
          const option = action.options.find((o) => String(o.id) === sent);
          showToast(`已提交：${option ? option.label : sent}`);
        } else if (res) {
          showToast(res.message || "提交失败");
        }
      }
      onChange();
      return accepted;
    }

    /**
     * G45: a click on an option of a choice card.  It only PICKS the option (nothing is sent): another option
     * changes the pick, the picked option again takes it back.  The conditions are checked here as well - the
     * page may lag one beat behind the server.
     */
    function pickOption(requestId, value) {
      syncScope();
      const seat = played();
      const request = findRequest(requestId);
      const action = seat && request ? View.requestAction(session(), seat, request) : null;
      if (!action || action.kind === "text") {
        showToast("这个请求现在不能作答");
        return false;
      }
      if (!action.enabled) {
        showToast(action.disabledReason);
        return false;
      }
      // (an answer on its way does not stop the person from picking again: the new pick is its own wish, see respond)
      const option = action.options.find((o) => String(o.id) === String(value));
      if (!option) return false;
      const id = String(option.id);
      if (st.picked.get(requestId) === id) st.picked.delete(requestId);
      else st.picked.set(requestId, id);
      onChange();
      return true;
    }

    /** G45: the card's 「提交」 / 「修改回答」 button of a choice card: sends the pick (and only the pick). */
    async function confirmPick(requestId) {
      syncScope();
      const seat = played();
      const request = findRequest(requestId);
      const action = seat && request ? View.requestAction(session(), seat, request) : null;
      if (!action || action.kind === "text") {
        showToast("这个请求现在不能作答");
        return false;
      }
      if (!action.enabled) {
        showToast(action.disabledReason);
        return false;
      }
      const pick = View.requestPick(action, st.picked.get(requestId));
      if (pick.picked === null) {
        showToast("先选一个选项，再提交");
        return false;
      }
      if (!pick.dirty) {
        showToast("你已经提交了这个选择；要改的话，先选另一个选项");
        return false;
      }
      return respond(requestId, pick.picked);
    }

    function submitText(requestId) {
      syncScope();
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
        pickOption(opt.getAttribute("data-req-answer"), opt.getAttribute("data-value")); // G45: picks, sends nothing
        return;
      }
      const confirm = closestFrom(evt && evt.target, "[data-req-confirm]");
      if (confirm) {
        confirmPick(confirm.getAttribute("data-req-confirm"));
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
      pickOption,
      confirmPick,
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
