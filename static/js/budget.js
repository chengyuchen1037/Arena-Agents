/* ==========================================================================
   Arena-Agents v0.12.1 — token / cost budget dialog (E10, E22)
   Loaded after roster.js and before app.js. Exposes window.ArenaBudgetUI.create(ctx).

   The "上限" button next to the top-bar usage pill opens this dialog:
     - token limit and cost limit (both may stay empty = no limit)
     - "达到上限后强制停止" (pause the game when 100 % is reached)
     - the current usage with progress bars and the four token kinds
   Server API: GET / PUT api/budget  {max_tokens, max_cost_usd, force_stop}
   The live numbers (budget.updated / .warning / .reached events) live in the session
   (session.budget); app.js passes every change to onSession().

   ctx: { api, Core, View, showToast, getSession, onBudget(budget) }
   ========================================================================== */

(function (root) {
  "use strict";

  function create(ctx) {
    const opts = ctx || {};
    const Core = opts.Core || root.ArenaCore;
    const View = opts.View || root.ArenaView;
    const api = opts.api;
    const doc = opts.document || root.document;
    const showToast = opts.showToast || (() => {});
    const getSession = opts.getSession || (() => null);
    const onBudget = opts.onBudget || (() => {});
    if (!Core || !View || !doc) return null;

    const esc = Core.escapeHtml;
    const $ = (id) => (typeof doc.getElementById === "function" ? doc.getElementById(id) : null);

    const el = {
      modal: $("budget-modal"),
      usage: $("budget-usage"),
      breakdown: $("budget-breakdown"),
      inputTokens: $("input-budget-tokens"),
      inputCost: $("input-budget-cost"),
      chkForce: $("chk-budget-force-stop"),
      error: $("budget-form-error"),
      btnOpen: $("btn-open-budget-modal"),
      btnClose: $("btn-close-budget-modal"),
      btnCancel: $("btn-cancel-budget"),
      btnSave: $("btn-save-budget"),
    };

    const st = { open: false, busy: false, lastUsageHtml: null, lastBreakdownHtml: null };

    const session = () => getSession() || {};

    function setError(text) {
      if (!el.error) return;
      const t = Array.isArray(text) ? text.join("；") : String(text || "");
      el.error.textContent = t;
      el.error.hidden = !t;
    }

    function isOpen() {
      return Boolean(el.modal && el.modal.style && el.modal.style.display === "flex");
    }

    // -----------------------------------------------------------------------
    // rendering
    // -----------------------------------------------------------------------

    function barHtml(label, used, max, ratio, fmt) {
      if (!max) {
        return (
          `<div class="budget-row"><div class="budget-row-head"><span>${esc(label)}</span>` +
          `<span class="budget-nums">${esc(fmt(used))} <span class="muted">· 未设置上限</span></span></div>` +
          `<div class="budget-bar"><div class="budget-bar-fill none" style="width:0%"></div></div></div>`
        );
      }
      const cls = ratio >= 1 ? "reached" : ratio >= Core.BUDGET_WARN_RATIO ? "warning" : "ok";
      const width = Math.max(0, Math.min(100, ratio * 100));
      return (
        `<div class="budget-row"><div class="budget-row-head"><span>${esc(label)}</span>` +
        `<span class="budget-nums">${esc(fmt(used))} / ${esc(fmt(max))} · <strong class="budget-pct ${cls}">${esc(Core.formatPercent(ratio))}</strong></span></div>` +
        `<div class="budget-bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(width)}">` +
        `<div class="budget-bar-fill ${cls}" style="width:${width.toFixed(1)}%"></div></div></div>`
      );
    }

    function stateChipHtml(view) {
      const text =
        view.state === "reached"
          ? "已达到上限"
          : view.state === "warning"
            ? "已用 80% 以上"
            : view.hasLimit
              ? "正常"
              : "未设置上限";
      return `<span class="budget-chip ${view.state}">${esc(text)}</span>`;
    }

    function renderLive() {
      const s = session();
      const view = View.budgetView(s);
      const usageHtml =
        `<div class="budget-usage-head"><span class="budget-usage-title">当前用量（本局）</span>${stateChipHtml(view)}</div>` +
        barHtml("token", view.tokens.used, view.tokens.max, view.tokens.ratio || 0, (n) => Core.formatCount(n)) +
        barHtml("费用", view.cost.used, view.cost.max, view.cost.ratio || 0, (n) => Core.formatCost(n, 4));
      if (el.usage && usageHtml !== st.lastUsageHtml) {
        st.lastUsageHtml = usageHtml;
        el.usage.innerHTML = usageHtml;
      }
      const u = s.usage_stats || {};
      const cell = (label, n, cls) =>
        `<div class="pop-stat pop-tok ${cls}"><span class="pop-stat-num">${esc(Core.formatCount(n))}</span><span class="pop-stat-label">${esc(label)}</span></div>`;
      const breakdownHtml =
        `<div class="budget-breakdown-title">token 明细</div><div class="pop-grid budget-grid">` +
        cell("未命中输入", u.prompt_tokens, "pop-tok-miss") +
        cell("缓存读取", u.cache_read_tokens, "pop-tok-read") +
        cell("缓存写入", u.cache_write_tokens, "pop-tok-write") +
        cell("输出", u.completion_tokens, "pop-tok-out") +
        `</div><div class="form-hint">缓存命中率 ${esc(Core.formatPercent(u.cache_hit_rate))} · 合计 ${esc(Core.formatCount(u.total_tokens))} tokens · ${esc(Core.formatCost(u.total_cost_usd, 4))}</div>`;
      if (el.breakdown && breakdownHtml !== st.lastBreakdownHtml) {
        st.lastBreakdownHtml = breakdownHtml;
        el.breakdown.innerHTML = breakdownHtml;
      }
    }

    /** The limits go into the inputs (only when the dialog is opened or the server changed them). */
    function fillInputs(budget) {
      const b = Core.normalizeBudget(budget);
      if (el.inputTokens) el.inputTokens.value = b.max_tokens ? String(b.max_tokens) : "";
      if (el.inputCost) el.inputCost.value = b.max_cost_usd ? String(b.max_cost_usd) : "";
      if (el.chkForce) el.chkForce.checked = b.force_stop;
    }

    // -----------------------------------------------------------------------
    // actions
    // -----------------------------------------------------------------------

    function show() {
      if (el.modal) el.modal.style.display = "flex";
      st.open = true;
    }

    function hide() {
      if (el.modal) el.modal.style.display = "none";
      st.open = false;
    }

    /** The server answers with the budget itself (or something else: read it again). */
    async function adopt(res) {
      let b = res && typeof res === "object" && ("used_tokens" in res || "state" in res || "max_tokens" in res) ? res : null;
      if (!b) b = await api.get("api/budget");
      const budget = Core.normalizeBudget(b);
      onBudget(budget);
      return budget;
    }

    async function openDialog() {
      setError("");
      st.lastUsageHtml = null;
      st.lastBreakdownHtml = null;
      // show what we have right away; the fresh values follow
      fillInputs(session().budget);
      renderLive();
      show();
      try {
        const budget = await adopt(await api.get("api/budget"));
        fillInputs(budget);
        renderLive();
      } catch (e) {
        setError(`读取上限失败：${e.message}`);
      }
    }

    async function save() {
      if (st.busy) return;
      const built = Core.buildBudgetPayload({
        max_tokens: el.inputTokens ? el.inputTokens.value : "",
        max_cost_usd: el.inputCost ? el.inputCost.value : "",
        force_stop: el.chkForce ? el.chkForce.checked : false,
      });
      if (!built.ok) {
        setError(built.errors);
        return;
      }
      setError("");
      st.busy = true;
      if (el.btnSave) el.btnSave.disabled = true;
      try {
        const res = await api.put("api/budget", built.payload);
        const budget = await adopt(res);
        const parts = [];
        parts.push(budget.max_tokens ? `token 上限 ${Core.formatCount(budget.max_tokens)}` : "token 不限");
        parts.push(budget.max_cost_usd ? `费用上限 ${Core.formatCost(budget.max_cost_usd, 2)}` : "费用不限");
        showToast(`上限已保存：${parts.join("，")}${budget.force_stop ? "，达到后强制停止" : ""}`);
        hide();
      } catch (e) {
        setError(e.message);
        showToast(e.message);
      } finally {
        st.busy = false;
        if (el.btnSave) el.btnSave.disabled = false;
      }
    }

    /** Live session updates (budget.* events, metrics) repaint the open dialog. */
    function onSession() {
      if (isOpen()) renderLive();
    }

    function on(node, type, handler) {
      if (node && typeof node.addEventListener === "function") node.addEventListener(type, handler);
    }

    on(el.btnOpen, "click", () => openDialog());
    on(el.btnClose, "click", hide);
    on(el.btnCancel, "click", hide);
    on(el.btnSave, "click", save);
    on(el.modal, "keydown", (evt) => {
      if (evt && evt.key === "Enter" && evt.target && evt.target.tagName === "INPUT") {
        if (typeof evt.preventDefault === "function") evt.preventDefault();
        save();
      }
    });

    return { open: openDialog, close: hide, save, onSession, isOpen, _state: st };
  }

  root.ArenaBudgetUI = { create };
})(typeof window !== "undefined" ? window : globalThis);
