/* ==========================================================================
   Arena-Agents v0.11 — Workspace (工作文件夹) UI
   - top-bar button showing the current workspace name
   - modal: current workspace, recent workspaces, native folder picker
     (POST /api/workspaces/pick-native-folder), manual absolute-path fallback,
     switch confirmation (POST /api/workspaces/select) and loading a game from
     the current workspace (reuses the existing /api/sessions endpoints).
   Switching is refused while a game runs; after a successful switch the page is
   reloaded so provider/template/binding/SSE state is rebuilt from scratch.
   Loaded after core.js; app.js calls window.ArenaWorkspaceUI.create().
   ========================================================================== */

(function (root) {
  "use strict";

  function create(ctx) {
    const opts = ctx || {};
    const api = opts.api;
    const Core = opts.Core || root.ArenaCore;
    const doc = opts.document || root.document;
    const win = opts.window || root;
    const showToast = opts.showToast || (() => {});
    const getSession = opts.getSession || (() => null);
    const onSessionLoaded = opts.onSessionLoaded || null;
    const doReload =
      opts.reload ||
      (() => {
        try {
          win.location.reload();
        } catch {
          /* ignore: nowhere to reload in the test DOM */
        }
      });
    if (!Core || !doc) return null;

    const $ = (id) => (typeof doc.getElementById === "function" ? doc.getElementById(id) : null);
    const esc = Core.escapeHtml;
    const enc = encodeURIComponent;

    const el = {
      btnOpen: $("btn-open-workspace-modal"),
      nameLabel: $("workspace-name-label"),
      modal: $("workspace-modal"),
      currentName: $("workspace-current-name"),
      currentPath: $("workspace-current-path"),
      currentMeta: $("workspace-current-meta"),
      modeHint: $("workspace-mode-hint"),
      recentList: $("workspace-recent-list"),
      btnPickNative: $("btn-pick-native-folder"),
      pickerStatus: $("workspace-picker-status"),
      pathInput: $("input-workspace-path"),
      chkInitExisting: $("chk-workspace-initialize-existing"),
      chkCreateMissing: $("chk-workspace-create-missing"),
      formError: $("workspace-form-error"),
      btnSwitch: $("btn-switch-workspace"),
      switchHint: $("workspace-switch-hint"),
      sessionsList: $("workspace-session-list"),
      sessionsHint: $("workspace-sessions-hint"),
      btnRefreshSessions: $("btn-refresh-workspace-sessions"),
      btnClose: $("btn-close-workspace-modal"),
    };

    const st = {
      info: Core.normalizeWorkspaces(null), // enabled/current/recent/default_path/native_picker
      sessions: [],
      loaded: false,
      lastError: "",
      busy: false,
      sessionsLoading: false,
    };

    const isRunning = () => Core.workspaceSwitchBlocked(getSession());
    const modalOpen = () =>
      Boolean(el.modal && el.modal.style && el.modal.style.display === "flex");

    function call(node, method, ...args) {
      if (node && typeof node[method] === "function") {
        try {
          node[method](...args);
        } catch {
          /* ignore */
        }
      }
    }

    function setText(node, text) {
      if (node) node.textContent = String(text);
    }

    function setError(text) {
      if (!el.formError) return;
      el.formError.textContent = text || "";
      el.formError.hidden = !text;
    }

    function setPickerStatus(text) {
      setText(el.pickerStatus, text || "");
    }

    function setSwitchHint(text) {
      setText(el.switchHint, text || "");
    }

    function setSessionsHint(text) {
      setText(el.sessionsHint, text || "");
    }

    // -----------------------------------------------------------------------
    // Top-bar label
    // -----------------------------------------------------------------------
    function currentInfo() {
      return st.info.current || null;
    }

    function setTopbarLabel() {
      const info = currentInfo();
      const label = info && info.name ? info.name : Core.WORKSPACE_DEFAULT_LABEL;
      setText(el.nameLabel, label);
      if (el.btnOpen && typeof el.btnOpen.setAttribute === "function") {
        const pathText = info && info.path ? `当前：${info.path}` : "选择工作文件夹";
        el.btnOpen.setAttribute("title", `工作文件夹：${pathText}`);
      }
    }

    /** /api/state carries a workspace summary; use it for the topbar label. */
    function onState(workspaceInfo) {
      const info = Core.normalizeWorkspaceInfo(workspaceInfo);
      if (!info) return false;
      st.info.current = info;
      st.loaded = true;
      setTopbarLabel();
      if (modalOpen()) {
        renderCurrent();
        renderLockState();
      }
      return true;
    }

    // -----------------------------------------------------------------------
    // Loading
    // -----------------------------------------------------------------------
    async function load() {
      try {
        const res = await api.get("api/workspaces");
        st.info = Core.normalizeWorkspaces(res);
        st.lastError = "";
        st.loaded = true;
        setTopbarLabel();
        return { ok: true };
      } catch (e) {
        st.lastError = e.message || String(e);
        setTopbarLabel();
        return { ok: false, error: e };
      }
    }

    async function refreshAll() {
      await load();
      renderAll();
      await loadSessions();
      return st.info;
    }

    // -----------------------------------------------------------------------
    // Rendering
    // -----------------------------------------------------------------------
    function renderCurrent() {
      const info = currentInfo();
      if (el.currentName) {
        setText(el.currentName, info ? info.name : st.loaded ? "未知工作区" : "读取中…");
      }
      if (el.currentPath) {
        const path = info
          ? Core.workspacePathDisplay(info)
          : st.info.default_path || "—";
        setText(el.currentPath, path || "—");
      }
      if (el.currentMeta) {
        const parts = [];
        if (info && info.status) parts.push(`状态：${info.status}`);
        if (info && info.last_session) parts.push(`上次对局：${info.last_session}`);
        if (info && info.degraded) parts.push("⚠ 工作区处于降级/需恢复状态");
        if (info && info.id) parts.push(`ID：${info.id}`);
        setText(el.currentMeta, parts.join(" · "));
      }
      if (el.modeHint) {
        if (!st.info.enabled) {
          setText(
            el.modeHint,
            "当前运行模式未启用工作文件夹（ARENA_DISABLE_WORKSPACE）：不能切换工作区。"
          );
        } else if (st.lastError) {
          setText(el.modeHint, `读取工作区信息失败：${st.lastError}（可手动输入绝对路径后重试）`);
        } else if (!st.loaded) {
          setText(el.modeHint, "正在读取工作区信息…");
        } else {
          setText(
            el.modeHint,
            "Provider、模型模板、模型绑定与对局存档都保存在当前工作文件夹内；切换后页面会自动刷新。"
          );
        }
      }
      // Offer the machine default as the manual-path placeholder when known.
      if (el.pathInput && st.info.default_path && !el.pathInput.value) {
        el.pathInput.placeholder = st.info.default_path;
      }
    }

    function renderRecent() {
      if (!el.recentList) return;
      const recent = (st.info.recent || []).filter((w) => w.path || w.name);
      if (recent.length === 0) {
        el.recentList.innerHTML = `<div class="empty-placeholder">暂无最近工作区</div>`;
        return;
      }
      el.recentList.innerHTML = recent
        .map((w) => {
          const path = w.path || "";
          const current = currentInfo() && path && currentInfo().path === path;
          return `
          <div class="workspace-recent-item${current ? " current" : ""}">
            <div class="workspace-recent-main">
              <div class="workspace-recent-name">${esc(w.name)}${
                current ? ' <span class="tag-badge muted">当前</span>' : ""
              }</div>
              <div class="workspace-recent-path">${esc(
                path || "（远程访问不显示完整路径）"
              )}</div>
            </div>
            <button type="button" class="btn btn-xs" data-action="use-workspace"
              data-path="${esc(path)}" ${path ? "" : "disabled"}>填入路径</button>
          </div>`;
        })
        .join("");
    }

    function renderLockState() {
      const running = isRunning();
      const enabled = st.info.enabled;
      const blocked = running || !enabled || st.busy;
      if (el.btnSwitch) el.btnSwitch.disabled = blocked;
      if (el.chkInitExisting) el.chkInitExisting.disabled = !enabled;
      if (el.chkCreateMissing) el.chkCreateMissing.disabled = !enabled;
      if (el.pathInput) el.pathInput.disabled = !enabled;
      if (el.btnPickNative) el.btnPickNative.disabled = !enabled || !st.info.native_picker;
      if (!enabled) {
        setSwitchHint("当前模式未启用工作文件夹，不能切换。");
      } else if (running) {
        setSwitchHint("对局运行中：请先点击「暂停」并等待保存完成，再切换工作区。");
      } else if (st.busy) {
        setSwitchHint("正在切换工作区…");
      } else {
        setSwitchHint("切换前会先保存当前对局；运行中必须先暂停。");
      }
      if (el.btnRefreshSessions) el.btnRefreshSessions.disabled = st.sessionsLoading;
    }

    function renderSessions() {
      if (!el.sessionsList) return;
      const running = isRunning();
      const session = getSession();
      const currentId = session && session.session_id;
      if (st.sessions.length === 0) {
        el.sessionsList.innerHTML = `<div class="empty-placeholder">本工作区还没有已保存的对局</div>`;
      } else {
        el.sessionsList.innerHTML = st.sessions
          .map((s) => {
            const isCurrent = Boolean(s.current || s.session_id === currentId);
            const meta = [s.title, s.status, s.updated_at || s.saved_at]
              .filter((v) => v !== undefined && v !== null && String(v) !== "")
              .map((v) => esc(v))
              .join(" &middot; ");
            return `
            <div class="session-item" data-session-id="${esc(s.session_id)}">
              <div class="session-item-main">
                <div class="session-item-id">${esc(s.session_id)}${
                  isCurrent ? ' <span class="tag-badge muted">当前</span>' : ""
                }</div>
                <div class="session-item-meta">${meta}</div>
              </div>
              <button type="button" class="btn btn-xs" data-action="load-session" ${
                isCurrent || running ? "disabled" : ""
              } title="${running ? "对局运行中不能切换存档，请先暂停" : ""}">载入</button>
            </div>`;
          })
          .join("");
      }
      if (running) {
        setSessionsHint("对局运行中不能切换存档，请先暂停。");
      } else {
        setSessionsHint("加载后对局保持暂停，请检查模型绑定后再点击「继续」。");
      }
      renderLockState();
    }

    function renderAll() {
      renderCurrent();
      renderRecent();
      renderLockState();
      renderSessions();
    }

    // -----------------------------------------------------------------------
    // Actions
    // -----------------------------------------------------------------------
    async function loadSessions() {
      st.sessionsLoading = true;
      if (el.sessionsHint) setSessionsHint("正在读取本工作区对局…");
      renderLockState();
      try {
        const res = await api.get("api/sessions");
        st.sessions = Core.normalizeSessions(res);
      } catch (e) {
        st.sessions = [];
        setSessionsHint(`读取对局列表失败：${e.message}`);
      } finally {
        st.sessionsLoading = false;
      }
      renderSessions();
      return st.sessions;
    }

    function setPathValue(path) {
      if (!el.pathInput) return;
      el.pathInput.value = path || "";
    }

    function fillRecentPath(path) {
      if (!path) {
        showToast("远程访问不返回工作区完整路径，无法填入；请手动输入绝对路径");
        return;
      }
      const checked = Core.validateWorkspacePath(path);
      if (!checked.ok) {
        showToast(checked.error);
        return;
      }
      setPathValue(checked.value);
      setError("");
      setPickerStatus(`已填入最近工作区：${checked.value}`);
    }

    async function pickNativeFolder() {
      if (!st.info.enabled) {
        showToast("当前模式未启用工作文件夹");
        return;
      }
      setPickerStatus("正在打开服务器本机的文件夹选择器…");
      try {
        const res = await api.post("api/workspaces/pick-native-folder");
        const path = res && typeof res.path === "string" ? res.path.trim() : "";
        if (!path) {
          setPickerStatus("没有选择文件夹；也可以直接手动输入绝对路径");
          return;
        }
        setPathValue(path);
        setError("");
        setPickerStatus(`已选择：${path}`);
      } catch (e) {
        // Native picker is loopback-only and needs a graphical session:
        // always offer the manual absolute-path input as the fallback.
        setPickerStatus(
          `无法打开本机文件夹选择器（${e.message}）。请手动输入工作文件夹的绝对路径。`
        );
        showToast("无法打开本机选择器，请手动输入绝对路径");
      }
    }

    function setBusy(busy) {
      st.busy = busy;
      renderLockState();
    }

    async function switchWorkspace() {
      if (isRunning()) {
        showToast("对局运行中，不能切换工作区：请先暂停并等待保存完成");
        setSwitchHint("对局运行中：请先点击「暂停」并等待保存完成，再切换工作区。");
        return;
      }
      if (!st.info.enabled) {
        showToast("当前模式未启用工作文件夹，不能切换");
        return;
      }
      const built = Core.buildWorkspaceSelectPayload({
        path: el.pathInput ? el.pathInput.value : "",
        initialize_existing: Boolean(el.chkInitExisting && el.chkInitExisting.checked),
        create_if_missing: Boolean(el.chkCreateMissing && el.chkCreateMissing.checked),
      });
      if (!built.ok) {
        setError(built.error);
        return;
      }
      setError("");
      const path = built.payload.path;
      const confirmed =
        typeof root.confirm !== "function" ||
        root.confirm(
          `确定切换到工作文件夹「${path}」吗？\n\n` +
            "切换前会先保存当前对局（运行中必须先暂停）；" +
            "切换成功后页面会自动刷新以重新加载 Provider、模板与对局。"
        );
      if (!confirmed) return;
      setBusy(true);
      try {
        const res = await api.post("api/workspaces/select", built.payload);
        const info = Core.normalizeWorkspaceInfo(res && res.workspace);
        if (info) st.info.current = info;
        setTopbarLabel();
        renderCurrent();
        setSwitchHint("工作区已切换，正在刷新页面…");
        showToast("工作区已切换，页面即将刷新");
        doReload();
      } catch (e) {
        setError(`切换失败：${e.message}`);
        showToast(`切换工作区失败：${e.message}`);
      } finally {
        setBusy(false);
      }
    }

    async function loadSession(id) {
      if (isRunning()) {
        showToast("对局运行中不能切换存档，请先暂停");
        return;
      }
      const confirmed =
        typeof root.confirm !== "function" ||
        root.confirm(
          `加载对局「${id}」会替换当前对局（当前对局会先自动保存），确定吗？`
        );
      if (!confirmed) return;
      try {
        const res = await api.post(`api/sessions/${enc(id)}/load`);
        if (res && res.session && onSessionLoaded) onSessionLoaded(res.session);
        hideModal();
        showToast("对局已载入（保持暂停，请检查模型绑定后点击「继续」）");
      } catch (e) {
        showToast(e.message);
      }
    }

    // -----------------------------------------------------------------------
    // Modal open / close + wiring
    // -----------------------------------------------------------------------
    function showModal() {
      if (el.modal) el.modal.style.display = "flex";
    }

    function hideModal() {
      if (el.modal) el.modal.style.display = "none";
    }

    function resetForm() {
      setError("");
      setPickerStatus(
        st.info.native_picker
          ? "仅本机可打开系统选择器；远程访问请手动输入绝对路径"
          : "当前部署不支持系统文件夹选择器，请手动输入绝对路径"
      );
      setSwitchHint("");
      setPathValue("");
    }

    async function openModal() {
      showModal();
      resetForm();
      renderAll();
      await refreshAll();
      return st.info;
    }

    function on(node, type, handler) {
      if (node && typeof node.addEventListener === "function") node.addEventListener(type, handler);
    }

    function closestAttr(target, selector, attr) {
      const node = target && typeof target.closest === "function" ? target.closest(selector) : null;
      return node ? node.getAttribute(attr) : null;
    }

    on(el.btnOpen, "click", () => openModal());
    on(el.btnClose, "click", () => hideModal());
    on(el.btnPickNative, "click", () => pickNativeFolder());
    on(el.btnSwitch, "click", () => switchWorkspace());
    on(el.btnRefreshSessions, "click", () => loadSessions());
    on(el.chkInitExisting, "change", () => renderLockState());
    on(el.chkCreateMissing, "change", () => renderLockState());

    on(el.recentList, "click", (evt) => {
      const action = closestAttr(evt.target, "[data-action]", "data-action");
      const path = closestAttr(evt.target, "[data-action]", "data-path");
      if (action === "use-workspace") fillRecentPath(path);
    });

    on(el.sessionsList, "click", (evt) => {
      const action = closestAttr(evt.target, "[data-action]", "data-action");
      const id = closestAttr(evt.target, ".session-item[data-session-id]", "data-session-id");
      if (action === "load-session" && id) loadSession(id);
    });

    setTopbarLabel();
    renderAll();

    return {
      load,
      onState,
      onSession: () => {
        // Live session updates (SSE) must flip the running lock immediately.
        if (modalOpen()) {
          renderLockState();
          renderSessions();
        }
        return isRunning();
      },
      openModal,
      closeModal: hideModal,
      refresh: refreshAll,
      reloadSessions: loadSessions,
      getInfo: () => st.info,
      isRunning,
      _state: st,
    };
  }

  root.ArenaWorkspaceUI = { create };
})(typeof window !== "undefined" ? window : globalThis);
