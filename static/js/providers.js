/* ==========================================================================
   Arena-Agents v0.10 — Provider / model / binding / session-list UI
   Loaded after core.js and before app.js. Exposes window.ArenaProviderUI.create(ctx).
   ctx: { api, Core, showToast, getSession, onChange }
   ========================================================================== */

(function (root) {
  "use strict";

  function create(ctx) {
    const { api, Core, showToast, getSession } = ctx;
    const onChange = ctx.onChange || (() => {});
    const esc = Core.escapeHtml;
    const enc = encodeURIComponent;
    const $ = (id) => document.getElementById(id);

    const el = {
      providerModal: $("provider-modal"),
      providerList: $("provider-list"),
      btnNewProvider: $("btn-new-provider"),
      form: $("provider-form"),
      name: $("input-provider-name"),
      protocol: $("select-provider-protocol"),
      baseUrl: $("input-provider-base-url"),
      autoAppend: $("chk-auto-append"),
      autoAppendHint: $("auto-append-hint"),
      endpointPreview: $("endpoint-preview"),
      previewStatus: $("endpoint-preview-status"),
      previewMessage: $("endpoint-preview-message-url"),
      previewModels: $("endpoint-preview-models-url"),
      previewWarnings: $("endpoint-preview-warnings"),
      key: $("input-provider-key"),
      keyHint: $("hint-provider-key"),
      formError: $("provider-form-error"),
      btnSaveProvider: $("btn-save-provider"),
      btnDeleteProvider: $("btn-delete-provider"),
      modelsSection: $("provider-models-section"),
      modelList: $("provider-model-list"),
      btnDiscover: $("btn-discover-models"),
      discoverStatus: $("discover-status"),
      discoverList: $("discover-list"),
      modelForm: $("model-form"),
      modelId: $("input-model-id"),
      priceIn: $("input-model-price-in"),
      priceOut: $("input-model-price-out"),
      ctxWindow: $("input-model-ctx"),
      modelError: $("model-form-error"),
      btnSubmitModel: $("btn-submit-model"),

      bindingsModal: $("bindings-modal"),
      bindingsTable: $("bindings-table"),
      bindingsHint: $("bindings-lock-hint"),
      bindingsIssues: $("bindings-issues"),
      btnApplyAll: $("btn-bindings-apply-all"),
      btnSaveBindings: $("btn-save-bindings"),

      templatesSection: $("templates-section"),
      templatesList: $("template-slots"),
      templatesError: $("templates-error"),
      templatesLockHint: $("templates-lock-hint"),
      btnSaveTemplates: $("btn-save-templates"),
      btnSelectAllActors: $("btn-select-all-actors"),
      btnSelectNoneActors: $("btn-select-none-actors"),
      templateApplyHint: $("template-apply-hint"),

      sessionsModal: $("sessions-modal"),
      sessionList: $("session-list"),

      statusDot: $("api-status-dot"),
      statusLabel: $("api-status-label"),

      notice: $("restore-notice"),
      noticeText: $("restore-notice-text"),
    };

    const st = {
      providers: [],
      bindings: {}, // server defaults from GET /settings/bindings
      selectedId: null, // provider id | "new" | null
      discovered: [],
      rows: [],
      sessions: [],
      dismissedNotice: "",
      lastSig: "",
      // v0.11: endpoint preview + auto_append_path
      preview: Core.normalizeEndpointPreview(null),
      previewSig: "",
      previewSeq: 0,
      previewTimer: null,
      // v0.11: 3-slot model templates
      templates: Core.normalizeTemplates(null),
      templateRows: Core.templateRows(Core.normalizeTemplates(null)),
      templateErrors: [],
      templatesAvailable: true,
    };

    const locked = () => Core.bindingsLocked(getSession());
    const current = () => Core.findProvider(st.providers, st.selectedId);
    const running = () => Boolean(getSession() && getSession().status === "running");

    function show(modal) {
      if (modal) modal.style.display = "flex";
    }
    function hide(modal) {
      if (modal) modal.style.display = "none";
    }
    function setError(box, errors) {
      if (!box) return;
      const text = Array.isArray(errors) ? errors.join("；") : String(errors || "");
      box.textContent = text;
      box.hidden = !text;
    }

    // -----------------------------------------------------------------------
    // v0.11: auto_append_path + endpoint preview (debounced, server-computed)
    // -----------------------------------------------------------------------

    function autoAppendEnabled() {
      return el.autoAppend ? Boolean(el.autoAppend.checked) : true;
    }

    function renderAutoAppendUi() {
      const auto = autoAppendEnabled();
      if (el.autoAppendHint) {
        el.autoAppendHint.textContent = auto
          ? "开启：Base URL 作为根地址，服务端自动补全 /v1/chat/completions、/v1/responses 或 /v1/messages。"
          : "关闭：Base URL 就是完整消息 endpoint，原样发送；无法自动获取模型，只能手动填写模型 ID。";
      }
      renderDiscoverAvailability();
    }

    function setPreviewStatus(text, isError) {
      if (!el.previewStatus) return;
      el.previewStatus.textContent = text || "";
      el.previewStatus.className = isError
        ? "endpoint-preview-status form-hint error"
        : "endpoint-preview-status form-hint";
    }

    function clearEndpointUrls() {
      if (el.previewMessage) el.previewMessage.textContent = "—";
      if (el.previewModels) el.previewModels.textContent = "—";
      if (el.previewWarnings) {
        el.previewWarnings.hidden = true;
        el.previewWarnings.innerHTML = "";
      }
    }

    function renderEndpointPreview() {
      const p = st.preview;
      if (!p || !p.request_url) {
        clearEndpointUrls();
        return;
      }
      if (el.previewMessage) el.previewMessage.textContent = p.request_url;
      if (el.previewModels) {
        el.previewModels.textContent = p.models_url
          ? p.models_url
          : "关闭自动拼接：无法自动获取模型，只能手动填写模型 ID";
      }
      if (el.previewWarnings) {
        const warnings = p.warnings || [];
        el.previewWarnings.hidden = warnings.length === 0;
        el.previewWarnings.innerHTML = warnings
          .map((w) => `<div>⚠ ${esc(w)}</div>`)
          .join("");
      }
    }

    function endpointFormSig() {
      return [
        el.protocol ? el.protocol.value : "",
        el.baseUrl ? String(el.baseUrl.value || "").trim() : "",
        autoAppendEnabled() ? "auto1" : "auto0",
      ].join("|");
    }

    function scheduleEndpointPreview(delay) {
      const wait = Number.isFinite(delay) ? delay : 320;
      if (st.previewTimer !== null && st.previewTimer !== undefined) {
        try {
          root.clearTimeout(st.previewTimer);
        } catch {
          /* ignore */
        }
        st.previewTimer = null;
      }
      st.previewTimer = root.setTimeout(() => {
        st.previewTimer = null;
        runEndpointPreview();
      }, wait);
    }

    async function runEndpointPreview() {
      if (!el.baseUrl) return;
      const built = Core.buildEndpointPreviewBody({
        protocol: el.protocol ? el.protocol.value : "",
        base_url: el.baseUrl.value,
        auto_append_path: autoAppendEnabled(),
      });
      if (!built.ok) {
        st.previewSeq += 1; // invalidate any in-flight response
        clearEndpointUrls();
        setPreviewStatus(built.error, true);
        return;
      }
      const sig = endpointFormSig();
      const seq = ++st.previewSeq;
      setPreviewStatus("正在计算最终请求地址…");
      try {
        const res = await api.post("api/providers/endpoint-preview", built.payload);
        if (seq !== st.previewSeq) return; // a newer edit won
        st.preview = Core.normalizeEndpointPreview(res);
        st.previewSig = sig;
        renderEndpointPreview();
        setPreviewStatus("最终地址由服务端计算（保存时后端会再校验一次）");
      } catch (e) {
        if (seq !== st.previewSeq) return;
        clearEndpointUrls();
        setPreviewStatus(`无法预览最终地址：${e.message}`, true);
      }
    }

    /** Re-run the preview only when the form's endpoint inputs actually changed. */
    function refreshEndpointPreview(delay) {
      if (!el.baseUrl || !el.protocol) return;
      const base = String(el.baseUrl.value || "").trim();
      if (!base) {
        st.previewSeq += 1;
        clearEndpointUrls();
        setPreviewStatus("填写 Base URL 后显示最终请求地址");
        return;
      }
      if (endpointFormSig() === st.previewSig) return;
      scheduleEndpointPreview(delay === undefined ? 320 : delay);
    }

    // -----------------------------------------------------------------------
    // Loading
    // -----------------------------------------------------------------------

    async function loadProviders() {
      const res = await api.get("api/providers");
      st.providers = Core.normalizeProviders(res);
    }

    async function loadBindings() {
      const res = await api.get("api/settings/bindings");
      st.bindings = (res && res.bindings && typeof res.bindings === "object" && res.bindings) || {};
      // Some deployments embed the provider list here; only use it if /providers is empty.
      if (st.providers.length === 0 && res && res.providers) {
        st.providers = Core.normalizeProviders(res.providers);
      }
    }

    /** Load providers + bindings at startup (401 reuses the shared token modal). */
    async function load() {
      try {
        await loadProviders();
        await loadBindings();
        refreshAll();
        return { ok: true };
      } catch (e) {
        console.error("Failed to load providers/bindings", e);
        if (el.statusLabel) el.statusLabel.textContent = "模型配置状态未知";
        return { ok: false, error: e };
      }
    }

    function refreshAll() {
      renderStatus();
      renderProviderPanel();
      if (el.bindingsModal && el.bindingsModal.style.display === "flex") renderBindings();
      onChange();
    }

    // -----------------------------------------------------------------------
    // Top-bar status (moderator binding)
    // -----------------------------------------------------------------------

    function effectiveBinding(actorId) {
      const s = getSession();
      const sb = (s && s.model_bindings) || {};
      return sb[actorId] || st.bindings[actorId] || null;
    }

    function renderStatus() {
      if (!el.statusLabel) return;
      const b = effectiveBinding("moderator");
      const provider = b ? Core.findProvider(st.providers, b.provider_id) : null;
      const model = provider ? Core.findModel(provider, b.model_id) : null;
      if (provider && model) {
        el.statusLabel.textContent = `${provider.name} / ${model.id}${
          provider.api_key_set ? "" : "（缺 Key）"
        }`;
        if (el.statusDot) {
          el.statusDot.className = provider.api_key_set ? "api-dot configured" : "api-dot";
        }
      } else {
        el.statusLabel.textContent = b ? "绑定失效，需重新绑定" : "未绑定模型";
        if (el.statusDot) el.statusDot.className = "api-dot";
      }
    }

    // -----------------------------------------------------------------------
    // Provider modal
    // -----------------------------------------------------------------------

    function fillProtocolOptions() {
      if (!el.protocol || el.protocol.__filled) return;
      el.protocol.innerHTML = Core.PROTOCOLS.map(
        (p) => `<option value="${esc(p.id)}">${esc(p.label)}</option>`
      ).join("");
      el.protocol.__filled = true;
    }

    function renderProviderList() {
      if (!el.providerList) return;
      const items = [];
      if (st.selectedId === "new") {
        items.push(
          `<div class="provider-item active" data-id="new"><div class="provider-item-name">（新 Provider）</div></div>`
        );
      }
      st.providers.forEach((p) => {
        const auto = Core.providerAutoAppend(p);
        items.push(`
          <div class="provider-item${p.id === st.selectedId ? " active" : ""}" data-id="${esc(p.id)}">
            <div class="provider-item-name">${esc(p.name)}${
              p.read_only ? ' <span class="tag-badge muted">只读</span>' : ""
            }</div>
            <div class="provider-item-meta">${esc(Core.protocolLabel(p.protocol))} &middot; ${
              p.api_key_set ? "Key 已配置" : "Key 未配置"
            } &middot; ${Core.numText(p.models.length)} 个模型 &middot; ${
              auto ? "自动拼接路径" : "完整端点"
            }</div>
            <div class="provider-item-url">${esc(p.base_url)}</div>
          </div>`);
      });
      if (items.length === 0) {
        items.push(`<div class="empty-placeholder">尚无 Provider，点击下方「新增 Provider」</div>`);
      }
      el.providerList.innerHTML = items.join("");
    }

    function renderProviderForm() {
      fillProtocolOptions();
      const isNew = st.selectedId === "new";
      const p = current();
      const readOnly = Boolean(p && p.read_only);
      const running = locked();
      const lockAddr = readOnly || (!isNew && running);

      if (el.name) el.name.value = isNew || !p ? "" : p.name;
      if (el.protocol) el.protocol.value = isNew || !p ? Core.PROTOCOLS[0].id : p.protocol;
      if (el.baseUrl) el.baseUrl.value = isNew || !p ? "" : p.base_url;
      if (el.autoAppend) {
        el.autoAppend.checked = isNew || !p ? true : Core.providerAutoAppend(p);
        el.autoAppend.disabled = lockAddr;
      }
      if (el.key) el.key.value = "";

      if (el.name) el.name.disabled = readOnly;
      if (el.protocol) el.protocol.disabled = lockAddr;
      if (el.baseUrl) el.baseUrl.disabled = lockAddr;
      if (el.key) el.key.disabled = readOnly;
      if (el.btnSaveProvider) el.btnSaveProvider.disabled = readOnly || (!isNew && !p);
      if (el.btnDeleteProvider) {
        el.btnDeleteProvider.hidden = isNew || !p;
        el.btnDeleteProvider.disabled = readOnly || running;
      }
      if (el.keyHint) {
        if (readOnly) {
          el.keyHint.textContent = "环境变量 / Secret 引导的只读 Provider，不能在此编辑。";
        } else if (isNew || !p) {
          el.keyHint.textContent = "新建时必填。保存后不会再显示。";
        } else if (running && !readOnly) {
          el.keyHint.textContent = p.api_key_set
            ? "对局运行中：只能轮换 Key（留空表示不修改）；协议、Base URL 与自动拼接路径已锁定。"
            : "对局运行中：协议、Base URL 与自动拼接路径已锁定。";
        } else {
          el.keyHint.textContent = p.api_key_set
            ? "Key 已配置（出于安全不显示）。留空表示不修改；修改协议、Base URL 或自动拼接路径时必须重新输入。"
            : "Key 未配置，请填写。";
        }
      }
      setError(el.formError, "");

      renderAutoAppendUi();
      const formVisible =
        Boolean(el.providerModal) && el.providerModal.style.display === "flex";
      if (readOnly) {
        st.previewSeq += 1;
        clearEndpointUrls();
        setPreviewStatus("只读 Provider：Base URL 由环境变量提供，不能在此修改。");
      } else if (formVisible) {
        refreshEndpointPreview(0);
      }
    }

    function renderModelList() {
      if (!el.modelsSection) return;
      const p = current();
      const isNew = st.selectedId === "new";
      el.modelsSection.hidden = isNew || !p;
      if (isNew || !p) return;
      const readOnly = p.read_only;
      const running = locked();
      el.modelList.innerHTML =
        p.models.length === 0
          ? `<div class="empty-placeholder">尚未添加模型。先「获取模型」，单选一个并填写价格后添加。</div>`
          : p.models
              .map(
                (m) => `
          <div class="model-item" data-model-id="${esc(m.id)}">
            <span class="model-item-id">${esc(m.id)}</span>
            <span class="model-item-price">输入 $${Core.numText(m.input_price_per_m)} · 输出 $${Core.numText(
                  m.output_price_per_m
                )} / 1M · 窗口 ${Core.numText(m.context_window_tokens)}</span>
            <button type="button" class="btn btn-xs" data-action="edit-model" ${
              readOnly ? "disabled" : ""
            }>编辑价格</button>
            <button type="button" class="btn btn-xs btn-danger-outline" data-action="remove-model" ${
              readOnly || running ? "disabled" : ""
            } title="${running ? "对局运行中不能移除模型" : ""}">移除</button>
          </div>`
              )
              .join("");
      const lock = readOnly;
      [el.modelId, el.priceIn, el.priceOut, el.ctxWindow, el.btnSubmitModel].forEach((x) => {
        if (x) x.disabled = lock;
      });
      renderDiscoverAvailability();
      renderDiscover();
      updateModelMode();
    }

    /**
     * "获取模型" needs the server-side models URL: disabled when the provider is
     * read-only or when auto_append_path is off (the Base URL is then a complete
     * message endpoint and an explicit Model ID must be typed by hand).
     */
    function renderDiscoverAvailability() {
      const p = current();
      const readOnly = Boolean(p && p.read_only);
      const auto = autoAppendEnabled();
      if (el.btnDiscover) el.btnDiscover.disabled = readOnly || !auto;
      if (el.discoverStatus) {
        if (!auto) {
          el.discoverStatus.textContent = "关闭自动拼接时不能获取模型，请手动填写模型 ID。";
        } else if (el.discoverStatus.textContent === "关闭自动拼接时不能获取模型，请手动填写模型 ID。") {
          el.discoverStatus.textContent = "";
        }
      }
      return Boolean(el.btnDiscover && !el.btnDiscover.disabled);
    }

    function renderDiscover() {
      if (!el.discoverList) return;
      const p = current();
      const selected = el.modelId ? el.modelId.value.trim() : "";
      el.discoverList.innerHTML = st.discovered
        .map((id) => {
          const exists = p && Core.findModel(p, id);
          return `<label class="discover-item"><input type="radio" name="discover-model" value="${esc(
            id
          )}" ${id === selected ? "checked" : ""}/> <span>${esc(id)}</span>${
            exists ? ' <span class="tag-badge muted">已添加</span>' : ""
          }</label>`;
        })
        .join("");
    }

    function updateModelMode() {
      if (!el.btnSubmitModel || !el.modelId) return;
      const mode = Core.modelFormMode(current(), el.modelId.value.trim());
      el.btnSubmitModel.textContent = mode === "edit" ? "保存价格" : "添加模型";
      return mode;
    }

    function clearModelForm() {
      if (el.modelId) el.modelId.value = "";
      if (el.priceIn) el.priceIn.value = "";
      if (el.priceOut) el.priceOut.value = "";
      if (el.ctxWindow) el.ctxWindow.value = "";
      setError(el.modelError, "");
      updateModelMode();
    }

    /** Put one model into the form; existing models prefill prices for editing. */
    function pickModel(modelId) {
      const p = current();
      if (el.modelId) el.modelId.value = modelId;
      const existing = p ? Core.findModel(p, modelId) : null;
      if (existing) {
        if (el.priceIn) el.priceIn.value = String(existing.input_price_per_m);
        if (el.priceOut) el.priceOut.value = String(existing.output_price_per_m);
        if (el.ctxWindow) el.ctxWindow.value = String(existing.context_window_tokens ?? "");
      } else {
        // New model: prices must be typed explicitly, never carried over.
        if (el.priceIn) el.priceIn.value = "";
        if (el.priceOut) el.priceOut.value = "";
        if (el.ctxWindow) el.ctxWindow.value = "";
      }
      setError(el.modelError, "");
      updateModelMode();
    }

    function renderProviderPanel() {
      if (!st.selectedId && st.providers.length > 0) st.selectedId = st.providers[0].id;
      if (st.selectedId && st.selectedId !== "new" && !current()) {
        st.selectedId = st.providers.length ? st.providers[0].id : null;
      }
      if (!st.selectedId) st.selectedId = "new";
      renderProviderList();
      renderProviderForm();
      renderModelList();
    }

    function selectProvider(id) {
      st.selectedId = id;
      st.discovered = [];
      st.preview = Core.normalizeEndpointPreview(null);
      st.previewSig = "";
      if (el.discoverStatus) el.discoverStatus.textContent = "";
      clearModelForm();
      renderProviderPanel();
    }

    async function reloadProviders(keepSelection) {
      const prev = st.selectedId;
      await loadProviders();
      if (keepSelection && prev && prev !== "new" && Core.findProvider(st.providers, prev)) {
        st.selectedId = prev;
      }
      refreshAll();
    }

    async function openProviderModal() {
      try {
        await loadProviders();
      } catch (e) {
        showToast(`读取 Provider 失败：${e.message}`);
      }
      st.selectedId = st.providers.length ? st.providers[0].id : "new";
      st.discovered = [];
      st.preview = Core.normalizeEndpointPreview(null);
      st.previewSig = "";
      clearModelForm();
      show(el.providerModal);
      renderProviderPanel();
    }

    async function submitProvider(evt) {
      if (evt && evt.preventDefault) evt.preventDefault();
      const isNew = st.selectedId === "new";
      const existing = isNew ? null : current();
      const built = Core.buildProviderPayload(
        {
          name: el.name.value,
          protocol: el.protocol.value,
          base_url: el.baseUrl.value,
          api_key: el.key.value,
          auto_append_path: autoAppendEnabled(),
        },
        existing
      );
      if (!built.ok) {
        setError(el.formError, built.errors);
        return;
      }
      setError(el.formError, "");
      try {
        let saved;
        if (isNew) {
          saved = await api.post("api/providers", built.payload);
        } else {
          if (Object.keys(built.payload).length === 0) {
            showToast("没有需要保存的改动");
            return;
          }
          saved = await api.patch(`api/providers/${enc(existing.id)}`, built.payload);
        }
        await loadProviders();
        if (saved && saved.id && Core.findProvider(st.providers, saved.id)) {
          st.selectedId = saved.id;
        }
        if (el.key) el.key.value = "";
        refreshAll();
        showToast(isNew ? "Provider 已创建" : "Provider 已保存");
      } catch (e) {
        setError(el.formError, e.message);
        showToast(e.message);
      }
    }

    async function deleteProvider() {
      const p = current();
      if (!p || p.read_only) return;
      if (!root.confirm(`确定删除 Provider「${p.name}」及其全部模型吗？`)) return;
      try {
        await api.del(`api/providers/${enc(p.id)}`);
        st.selectedId = null;
        await loadProviders();
        await loadBindings().catch(() => {});
        st.discovered = [];
        clearModelForm();
        refreshAll();
        showToast("Provider 已删除");
      } catch (e) {
        showToast(e.message);
      }
    }

    async function discoverModels() {
      const p = current();
      if (!p) return;
      if (!autoAppendEnabled()) {
        showToast("关闭自动拼接时不能自动获取模型，请手动填写模型 ID");
        if (el.discoverStatus) {
          el.discoverStatus.textContent = "关闭自动拼接时不能获取模型，请手动填写模型 ID。";
        }
        return;
      }
      if (el.discoverStatus) el.discoverStatus.textContent = "正在获取模型…";
      if (el.btnDiscover) el.btnDiscover.disabled = true;
      try {
        const res = await api.post(`api/providers/${enc(p.id)}/discover-models`);
        st.discovered = Core.normalizeModelIds(res);
        if (el.discoverStatus) {
          el.discoverStatus.textContent = st.discovered.length
            ? `共获取 ${st.discovered.length} 个模型：每次单选一个，填写价格后添加。`
            : "服务商没有返回任何模型，可手动输入模型 ID。";
        }
        renderDiscover();
      } catch (e) {
        st.discovered = [];
        renderDiscover();
        if (el.discoverStatus) el.discoverStatus.textContent = `获取失败：${e.message}`;
        showToast(`获取模型失败：${e.message}`);
      } finally {
        renderDiscoverAvailability();
      }
    }

    async function submitModel(evt) {
      if (evt && evt.preventDefault) evt.preventDefault();
      const p = current();
      if (!p) return;
      const built = Core.buildModelPayload({
        model_id: el.modelId.value,
        input_price_per_m: el.priceIn.value,
        output_price_per_m: el.priceOut.value,
        context_window_tokens: el.ctxWindow.value,
      });
      if (!built.ok) {
        setError(el.modelError, built.errors);
        return;
      }
      setError(el.modelError, "");
      const mode = Core.modelFormMode(p, built.payload.model_id);
      try {
        if (mode === "edit") {
          await api.patch(`api/providers/${enc(p.id)}/models`, built.payload);
        } else {
          await api.post(`api/providers/${enc(p.id)}/models`, built.payload);
        }
        await reloadProviders(true);
        clearModelForm(); // next model needs fresh, explicit prices
        renderDiscover();
        showToast(mode === "edit" ? "模型价格已保存" : "模型已添加");
      } catch (e) {
        setError(el.modelError, e.message);
        showToast(e.message);
      }
    }

    async function removeModel(modelId) {
      const p = current();
      if (!p) return;
      if (!root.confirm(`确定移除模型「${modelId}」吗？`)) return;
      try {
        await api.del(`api/providers/${enc(p.id)}/models`, { model_id: modelId });
        await reloadProviders(true);
        showToast("模型已移除");
      } catch (e) {
        showToast(e.message);
      }
    }

    function closestAttr(target, selector, attr) {
      const node = target && target.closest ? target.closest(selector) : null;
      return node ? node.getAttribute(attr) : null;
    }

    function wireProviderModal() {
      if (el.btnNewProvider) el.btnNewProvider.addEventListener("click", () => selectProvider("new"));
      if (el.providerList) {
        el.providerList.addEventListener("click", (evt) => {
          const id = closestAttr(evt.target, ".provider-item[data-id]", "data-id");
          if (id) selectProvider(id);
        });
      }
      if (el.form) el.form.addEventListener("submit", submitProvider);
      if (el.btnDeleteProvider) el.btnDeleteProvider.addEventListener("click", deleteProvider);
      if (el.btnDiscover) el.btnDiscover.addEventListener("click", discoverModels);
      // v0.11: live endpoint preview (debounced) + auto_append_path toggle
      if (el.baseUrl) {
        el.baseUrl.addEventListener("input", () => refreshEndpointPreview(320));
      }
      if (el.protocol) {
        el.protocol.addEventListener("change", () => refreshEndpointPreview(120));
      }
      if (el.autoAppend) {
        el.autoAppend.addEventListener("change", () => {
          renderAutoAppendUi();
          renderModelList();
          refreshEndpointPreview(120);
        });
      }
      if (el.discoverList) {
        el.discoverList.addEventListener("change", (evt) => {
          const t = evt.target;
          if (t && t.value) pickModel(String(t.value));
        });
      }
      if (el.modelId) {
        el.modelId.addEventListener("input", () => {
          // Typing a manual id deselects the radio group and re-evaluates add/edit mode.
          renderDiscover();
          updateModelMode();
        });
      }
      if (el.modelForm) el.modelForm.addEventListener("submit", submitModel);
      if (el.modelList) {
        el.modelList.addEventListener("click", (evt) => {
          const modelId = closestAttr(evt.target, ".model-item[data-model-id]", "data-model-id");
          const action = closestAttr(evt.target, "[data-action]", "data-action");
          if (!modelId || !action) return;
          if (action === "edit-model") {
            pickModel(modelId);
            renderDiscover();
          } else if (action === "remove-model") {
            removeModel(modelId);
          }
        });
      }
      const close = $("btn-close-provider-modal");
      if (close) close.addEventListener("click", () => hide(el.providerModal));
      const open = $("btn-open-provider-modal");
      if (open) open.addEventListener("click", openProviderModal);
    }

    // -----------------------------------------------------------------------
    // Bindings modal
    // -----------------------------------------------------------------------

    function buildRows() {
      const s = getSession();
      const actors = [{ id: "moderator", label: "主持人" }].concat(
        ((s && s.participants) || []).map((p) => ({
          id: p.id,
          label: `${p.name}（${p.seat} 号 · ${p.id}）`,
        }))
      );
      return actors.map((a) => {
        const b = effectiveBinding(a.id) || {};
        return {
          actor_id: a.id,
          label: a.label,
          selected: false, // v0.11: checkbox used by "应用模板"
          provider_id: b.provider_id || "",
          model_id: b.model_id || "",
          reasoning_depth: b.reasoning_depth || "high",
        };
      });
    }

    // -----------------------------------------------------------------------
    // Model templates (v0.11): 3 fixed slots, editable also while running,
    // applied to the checked actors (moderator included).
    // -----------------------------------------------------------------------

    function loadTemplatesFromResp(res) {
      st.templates = Core.normalizeTemplates(res);
      st.templateRows = Core.templateRows(st.templates);
      st.templatesAvailable = true;
    }

    async function loadTemplates() {
      try {
        const res = await api.get("api/settings/templates");
        loadTemplatesFromResp(res);
        st.templateErrors = [];
        return { ok: true };
      } catch (e) {
        // Older/partial backends may not expose the endpoint yet: the manual
        // binding UI keeps working, only template save/apply is unavailable.
        st.templatesAvailable = false;
        st.templateErrors = [`模板接口不可用：${e.message}`];
        return { ok: false, error: e };
      }
    }

    function setTemplateField(slot, field, value) {
      const row = st.templateRows.find((r) => r.slot === slot);
      if (!row) return null;
      row[field] = value;
      return row;
    }

    function renderSelectionHint() {
      if (!el.templateApplyHint) return;
      const count = Core.selectedActorIds(st.rows).length;
      el.templateApplyHint.textContent = locked()
        ? "对局运行中不能应用模板：模板可编辑，请暂停后再应用。"
        : count > 0
          ? `已勾选 ${count} 个角色（含主持人）`
          : "请勾选要应用模板的角色（含主持人）";
    }

    function renderTemplates() {
      if (!el.templatesList) return;
      const running = locked();
      el.templatesList.innerHTML = st.templateRows
        .map((row) => {
          const provider = Core.findProvider(st.providers, row.provider_id);
          const providerOpts = [`<option value="">— 未选择 —</option>`]
            .concat(
              st.providers.map(
                (p) =>
                  `<option value="${esc(p.id)}" ${
                    p.id === row.provider_id ? "selected" : ""
                  }>${esc(p.name)}</option>`
              )
            )
            .join("");
          const modelOpts = [`<option value="">— 未选择 —</option>`]
            .concat(
              (provider ? provider.models : []).map(
                (m) =>
                  `<option value="${esc(m.id)}" ${
                    m.id === row.model_id ? "selected" : ""
                  }>${esc(m.id)}</option>`
              )
            )
            .join("");
          const depthOpts = Core.REASONING_DEPTHS.map(
            (d) =>
              `<option value="${esc(d.id)}" ${
                d.id === row.reasoning_depth ? "selected" : ""
              }>${esc(d.label)}</option>`
          ).join("");
          const status = row.saved
            ? row.valid
              ? "已保存"
              : "⚠ 引用失效"
            : "未保存";
          const statusClass = row.saved ? (row.valid ? "ok" : "invalid") : "draft";
          return `
          <div class="template-row" data-slot="${row.slot}">
            <span class="template-slot-label">模板${row.slot}</span>
            <input type="text" class="form-input template-name" data-field="name"
              value="${esc(row.name)}" placeholder="模板${row.slot}名称"
              aria-label="模板${row.slot}名称" />
            <select class="form-input" data-field="provider_id" aria-label="模板${row.slot}的 Provider">${providerOpts}</select>
            <select class="form-input" data-field="model_id" aria-label="模板${row.slot}的模型">${modelOpts}</select>
            <select class="form-input" data-field="reasoning_depth" aria-label="模板${row.slot}的思考深度">${depthOpts}</select>
            <span class="template-status ${statusClass}">${status}</span>
          </div>`;
        })
        .join("");
      setError(el.templatesError, st.templateErrors);
      if (el.templatesLockHint) {
        el.templatesLockHint.textContent = st.templatesAvailable
          ? running
            ? "对局运行中：模板可以编辑和保存，但不能应用；请先暂停。"
            : "编辑后先「保存模板」，再勾选角色并点击「应用模板1/2/3」。"
          : "模板接口暂不可用（后端可能尚未升级），当前只能使用高级手动绑定。";
      }
      for (const slot of [1, 2, 3]) {
        const btn = $(`btn-apply-template-${slot}`);
        if (!btn) continue;
        const row = st.templateRows.find((r) => r.slot === slot);
        const complete = Core.templateRowComplete(row);
        btn.disabled = running || !st.templatesAvailable || !complete;
        btn.title = running
          ? "对局运行中不能应用模板，请先暂停"
          : complete
            ? `把模板${slot}应用到已勾选的角色`
            : `请先填写并保存模板${slot}`;
      }
      renderSelectionHint();
    }

    function onTemplateInput(evt) {
      const t = evt.target;
      if (!t || typeof t.getAttribute !== "function") return;
      const field = t.getAttribute("data-field");
      const slot = Number(closestAttr(t, ".template-row[data-slot]", "data-slot") || 0);
      if (!slot || field !== "name") return;
      setTemplateField(slot, "name", String(t.value || ""));
    }

    function onTemplateChange(evt) {
      const t = evt.target;
      if (!t || typeof t.getAttribute !== "function") return;
      const field = t.getAttribute("data-field");
      const slot = Number(closestAttr(t, ".template-row[data-slot]", "data-slot") || 0);
      if (!slot || !field) return;
      const row = st.templateRows.find((r) => r.slot === slot);
      if (!row) return;
      if (field === "provider_id") {
        row.provider_id = String(t.value || "");
        row.model_id = ""; // models belong to a provider: force an explicit re-pick
        row.valid = true;
        st.templateErrors = [];
        renderTemplates();
        return;
      }
      if (field === "model_id") {
        row.model_id = String(t.value || "");
        row.valid = true;
      } else if (field === "reasoning_depth") {
        row.reasoning_depth = String(t.value || "");
      } else if (field === "name") {
        row.name = String(t.value || "");
      }
      st.templateErrors = [];
      setError(el.templatesError, "");
    }

    function setAllSelected(selected) {
      st.rows.forEach((r) => {
        r.selected = Boolean(selected);
      });
      renderBindings();
    }

    async function saveTemplates() {
      if (!st.templatesAvailable) {
        showToast("模板接口暂不可用，无法保存模板");
        return;
      }
      const built = Core.buildTemplatesPayload(st.templateRows, st.providers);
      if (!built.ok) {
        st.templateErrors = built.errors;
        renderTemplates();
        showToast(built.errors[0]);
        return;
      }
      st.templateErrors = [];
      try {
        const res = await api.put("api/settings/templates", built.payload);
        loadTemplatesFromResp(res);
        st.templateErrors = [];
        renderTemplates();
        showToast("模型模板已保存");
      } catch (e) {
        st.templateErrors = [`保存模板失败：${e.message}`];
        renderTemplates();
        showToast(e.message);
      }
    }

    async function applyTemplate(slot) {
      if (locked()) {
        showToast("对局运行中不能应用模板：请先暂停对局");
        return;
      }
      if (!st.templatesAvailable) {
        showToast("模板接口暂不可用，无法应用模板");
        return;
      }
      const row = st.templateRows.find((r) => r.slot === slot);
      if (!Core.templateRowComplete(row)) {
        showToast(`模板${slot}还没有填写完整，请先保存模板`);
        return;
      }
      const actors = Core.selectedActorIds(st.rows);
      if (actors.length === 0) {
        showToast("请先勾选要应用模板的角色");
        return;
      }
      try {
        const res = await api.post(`api/settings/templates/${slot}/apply`, {
          actor_ids: actors,
        });
        const updated =
          res && res.bindings && typeof res.bindings === "object" ? res.bindings : {};
        st.bindings = { ...st.bindings, ...updated };
        st.rows = st.rows.map((r) => {
          const b = updated[r.actor_id];
          if (!b) return r;
          return {
            ...r,
            provider_id: b.provider_id || "",
            model_id: b.model_id || "",
            reasoning_depth: b.reasoning_depth || "high",
          };
        });
        renderBindings();
        renderStatus();
        onChange();
        showToast(`已把模板${slot}应用到 ${actors.length} 个角色`);
      } catch (e) {
        showToast(e.message);
      }
    }

    function renderBindings() {
      if (!el.bindingsTable) return;
      const running = locked();
      const dis = running ? "disabled" : "";
      if (el.bindingsHint) {
        el.bindingsHint.textContent = running
          ? "对局运行中：模型与思考深度已锁定，请先暂停后再修改。"
          : "思考深度（low/medium/high/xhigh）是模型的推理强度，与顶部的“执行深度”（工具调用预算）不同。";
      }
      if (el.btnSaveBindings) el.btnSaveBindings.disabled = running;
      if (el.btnApplyAll) el.btnApplyAll.disabled = running;
      renderTemplates();

      const s = getSession();
      const issues = (s && Array.isArray(s.binding_issues) && s.binding_issues) || [];
      if (el.bindingsIssues) {
        el.bindingsIssues.hidden = issues.length === 0;
        el.bindingsIssues.innerHTML = issues
          .map((i) => `<div>⚠ ${esc(i.actor_id)}：${esc(i.message)}</div>`)
          .join("");
      }

      el.bindingsTable.innerHTML = st.rows
        .map((r) => {
          const provider = Core.findProvider(st.providers, r.provider_id);
          const providerOpts = [`<option value="">— 未选择 —</option>`]
            .concat(
              st.providers.map(
                (p) =>
                  `<option value="${esc(p.id)}" ${p.id === r.provider_id ? "selected" : ""}>${esc(
                    p.name
                  )}</option>`
              )
            )
            .join("");
          const modelOpts = [`<option value="">— 未选择 —</option>`]
            .concat(
              (provider ? provider.models : []).map(
                (m) =>
                  `<option value="${esc(m.id)}" ${m.id === r.model_id ? "selected" : ""}>${esc(
                    m.id
                  )}</option>`
              )
            )
            .join("");
          const depthOpts = Core.REASONING_DEPTHS.map(
            (d) =>
              `<option value="${esc(d.id)}" ${d.id === r.reasoning_depth ? "selected" : ""}>${esc(
                d.label
              )}</option>`
          ).join("");
          return `
          <div class="binding-row${r.selected ? " selected" : ""}" data-actor="${esc(r.actor_id)}">
            <label class="binding-select" title="勾选后可用「应用模板」">
              <input type="checkbox" data-select-actor="${esc(r.actor_id)}" ${
                r.selected ? "checked" : ""
              } ${dis} aria-label="勾选 ${esc(r.label)}" />
            </label>
            <div class="binding-actor">${esc(r.label)}</div>
            <select class="form-input" data-field="provider_id" ${dis} aria-label="Provider">${providerOpts}</select>
            <select class="form-input" data-field="model_id" ${dis} aria-label="模型">${modelOpts}</select>
            <select class="form-input" data-field="reasoning_depth" ${dis} aria-label="思考深度">${depthOpts}</select>
          </div>`;
        })
        .join("");
    }

    function openBindings(actorId) {
      st.rows = buildRows();
      renderBindings();
      show(el.bindingsModal);
      if (actorId && el.bindingsTable && el.bindingsTable.querySelector) {
        const row = el.bindingsTable.querySelector(`.binding-row[data-actor="${actorId}"]`);
        if (row && row.scrollIntoView) row.scrollIntoView({ block: "center" });
      }
    }

    async function openBindingsFresh(actorId) {
      try {
        await loadProviders();
        await loadBindings();
        renderStatus();
        onChange();
      } catch (e) {
        showToast(`读取模型配置失败：${e.message}`);
      }
      await loadTemplates();
      openBindings(actorId);
    }

    function onBindingChange(evt) {
      const t = evt.target;
      const selectActor = t && typeof t.getAttribute === "function"
        ? t.getAttribute("data-select-actor")
        : null;
      if (selectActor) {
        const row = st.rows.find((r) => r.actor_id === selectActor);
        if (row) {
          row.selected = t.checked === undefined ? t.value === "true" : Boolean(t.checked);
          if (typeof t.closest === "function") {
            const rowEl = t.closest(".binding-row");
            if (rowEl && rowEl.classList) rowEl.classList.toggle("selected", row.selected);
          }
          renderSelectionHint();
        }
        return;
      }
      if (locked()) return;
      const field = t && typeof t.getAttribute === "function" ? t.getAttribute("data-field") : null;
      const actor = closestAttr(t, ".binding-row[data-actor]", "data-actor");
      if (!field || !actor) return;
      const row = st.rows.find((r) => r.actor_id === actor);
      if (!row) return;
      row[field] = String(t.value || "");
      if (field === "provider_id") {
        row.model_id = ""; // models belong to a provider: force an explicit re-pick
        renderBindings();
      }
    }

    function applyModeratorToAll() {
      if (locked()) return;
      const selectedByActor = new Map(st.rows.map((r) => [r.actor_id, Boolean(r.selected)]));
      st.rows = Core.applyModeratorToAll(st.rows).map((r) => ({
        ...r,
        selected: selectedByActor.get(r.actor_id) || false,
      }));
      renderBindings();
    }

    async function saveBindings() {
      if (locked()) {
        showToast("对局运行中，模型与思考深度已锁定");
        return;
      }
      const built = Core.buildBindingsPayload(st.rows, st.providers);
      if (!built.ok) {
        showToast(built.errors[0]);
        if (el.bindingsIssues) {
          el.bindingsIssues.hidden = false;
          el.bindingsIssues.innerHTML = built.errors.map((m) => `<div>⚠ ${esc(m)}</div>`).join("");
        }
        return;
      }
      try {
        const res = await api.put("api/settings/bindings", built.payload);
        st.bindings =
          (res && res.bindings && typeof res.bindings === "object" && res.bindings) ||
          built.payload.bindings;
        renderStatus();
        onChange();
        hide(el.bindingsModal);
        showToast("模型绑定已保存");
      } catch (e) {
        showToast(e.message);
      }
    }

    function wireBindingsModal() {
      if (el.bindingsTable) el.bindingsTable.addEventListener("change", onBindingChange);
      if (el.templatesList) {
        el.templatesList.addEventListener("input", onTemplateInput);
        el.templatesList.addEventListener("change", onTemplateChange);
      }
      if (el.btnSaveTemplates) el.btnSaveTemplates.addEventListener("click", saveTemplates);
      if (el.btnSelectAllActors) {
        el.btnSelectAllActors.addEventListener("click", () => setAllSelected(true));
      }
      if (el.btnSelectNoneActors) {
        el.btnSelectNoneActors.addEventListener("click", () => setAllSelected(false));
      }
      for (const slot of [1, 2, 3]) {
        const btn = $(`btn-apply-template-${slot}`);
        if (btn) btn.addEventListener("click", () => applyTemplate(slot));
      }
      if (el.btnApplyAll) el.btnApplyAll.addEventListener("click", applyModeratorToAll);
      if (el.btnSaveBindings) el.btnSaveBindings.addEventListener("click", saveBindings);
      const close = $("btn-close-bindings-modal");
      if (close) close.addEventListener("click", () => hide(el.bindingsModal));
      const cancel = $("btn-cancel-bindings");
      if (cancel) cancel.addEventListener("click", () => hide(el.bindingsModal));
      const open = $("btn-open-bindings-modal");
      if (open) open.addEventListener("click", () => openBindingsFresh());
    }

    // -----------------------------------------------------------------------
    // Sessions modal
    // -----------------------------------------------------------------------

    function renderSessions() {
      if (!el.sessionList) return;
      const s = getSession();
      const cur = s && s.session_id;
      if (st.sessions.length === 0) {
        el.sessionList.innerHTML = `<div class="empty-placeholder">没有已保存的外部存档</div>`;
        return;
      }
      el.sessionList.innerHTML = st.sessions
        .map((x) => {
          const meta = [x.title, x.status, x.updated_at || x.saved_at, x.record_count != null ? `${Core.numText(x.record_count)} 条记录` : ""]
            .filter((v) => v !== undefined && v !== null && String(v) !== "")
            .map((v) => esc(v))
            .join(" &middot; ");
          return `
          <div class="session-item" data-session-id="${esc(x.session_id)}">
            <div class="session-item-main">
              <div class="session-item-id">${esc(x.session_id)}${
                x.current || x.session_id === cur ? ' <span class="tag-badge muted">当前</span>' : ""
              }</div>
              <div class="session-item-meta">${meta}</div>
            </div>
            <button type="button" class="btn btn-xs" data-action="load-session" ${
              x.current || x.session_id === cur || locked() ? "disabled" : ""
            } title="${locked() ? "对局运行中不能切换存档，请先暂停" : ""}">加载</button>
            <button type="button" class="btn btn-xs btn-danger-outline" data-action="delete-session" ${
              x.current || x.session_id === cur ? "disabled" : ""
            }>删除</button>
          </div>`;
        })
        .join("");
    }

    async function loadSessions() {
      try {
        const res = await api.get("api/sessions");
        st.sessions = Core.normalizeSessions(res);
      } catch (e) {
        st.sessions = [];
        showToast(`读取存档列表失败：${e.message}`);
      }
      renderSessions();
    }

    async function loadSession(id) {
      if (!root.confirm(`加载存档「${id}」会替换当前对局（当前对局会先自动保存），确定吗？`)) return;
      try {
        const res = await api.post(`api/sessions/${enc(id)}/load`);
        if (res && res.session && ctx.onSessionLoaded) ctx.onSessionLoaded(res.session);
        hide(el.sessionsModal);
        showToast("存档已加载（对局保持暂停，请检查模型绑定后点击「继续」）");
      } catch (e) {
        showToast(e.message);
      }
    }

    async function deleteSession(id) {
      if (!root.confirm(`确定永久删除存档「${id}」吗？此操作不可恢复。`)) return;
      try {
        await api.del(`api/sessions/${enc(id)}`);
        showToast("存档已删除");
      } catch (e) {
        showToast(e.message);
      }
      await loadSessions();
    }

    function wireSessionsModal() {
      const open = $("btn-open-sessions-modal");
      if (open) {
        open.addEventListener("click", async () => {
          show(el.sessionsModal);
          await loadSessions();
        });
      }
      const close = $("btn-close-sessions-modal");
      if (close) close.addEventListener("click", () => hide(el.sessionsModal));
      const refresh = $("btn-refresh-sessions");
      if (refresh) refresh.addEventListener("click", loadSessions);
      if (el.sessionList) {
        el.sessionList.addEventListener("click", (evt) => {
          const action = closestAttr(evt.target, "[data-action]", "data-action");
          const id = closestAttr(evt.target, ".session-item[data-session-id]", "data-session-id");
          if (action === "delete-session" && id) deleteSession(id);
          else if (action === "load-session" && id) loadSession(id);
        });
      }
    }

    // -----------------------------------------------------------------------
    // Session-driven UI: restore notice, lock state
    // -----------------------------------------------------------------------

    function noticeFor(s) {
      if (!s) return "";
      const parts = [];
      if (s.restored_from_save) {
        parts.push(s.restore_notice || "已恢复上次对局（已暂停）");
      }
      const issues = Array.isArray(s.binding_issues) ? s.binding_issues : [];
      if (issues.length) {
        parts.push(
          `有 ${issues.length} 项模型绑定需要处理：` +
            issues
              .slice(0, 3)
              .map((i) => `${i.actor_id}（${i.message}）`)
              .join("；") +
            (issues.length > 3 ? "…" : "")
        );
      }
      return parts.join(" ");
    }

    function renderNotice(s) {
      if (!el.notice || !el.noticeText) return;
      const text = noticeFor(s);
      const visible = Boolean(text) && text !== st.dismissedNotice;
      el.noticeText.textContent = text;
      el.notice.hidden = !visible;
    }

    function onSession(s) {
      renderNotice(s);
      const sig = [
        s && s.status === "running" ? 1 : 0,
        JSON.stringify((s && s.model_bindings) || {}),
        s && s.participants ? s.participants.length : 0,
      ].join("|");
      if (sig === st.lastSig) return;
      st.lastSig = sig;
      renderStatus();
      if (el.bindingsModal && el.bindingsModal.style.display === "flex") renderBindings();
      if (el.providerModal && el.providerModal.style.display === "flex") renderProviderPanel();
    }

    function wireNotice() {
      const dismiss = $("btn-dismiss-notice");
      if (dismiss) {
        dismiss.addEventListener("click", () => {
          st.dismissedNotice = el.noticeText ? el.noticeText.textContent : "";
          if (el.notice) el.notice.hidden = true;
        });
      }
      const go = $("btn-notice-bindings");
      if (go) go.addEventListener("click", () => openBindingsFresh());
    }

    wireProviderModal();
    wireBindingsModal();
    wireSessionsModal();
    wireNotice();

    return {
      load,
      onSession,
      openBindings: openBindingsFresh,
      getProviders: () => st.providers,
      getBindingsFor: effectiveBinding,
      _state: st, // exposed for tests
    };
  }

  root.ArenaProviderUI = { create };
})(typeof window !== "undefined" ? window : globalThis);
