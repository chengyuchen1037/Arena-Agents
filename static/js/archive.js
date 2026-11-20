/* ==========================================================================
   Arena-Agents v0.13 — settings dialog: export / import an archive (decisions F24, F26-F32; v0.14: column ① of five, H35-H36)
   Loaded after bots.js / budget.js and before app.js. Exposes window.ArenaArchiveUI.create(ctx).

   Owns
     - the "⚙ 设置" dialog (#settings-modal): since v0.14 it has five columns (settings.js draws them; this file draws the home
       view's column ① 总存档 inside them, through `ctx.wrapHome`): 导出存档 and 导入存档, each a small wizard
         export: API key? -> password (typed twice, the rule is shown live) -> progress -> result + download
                 (the logs are always in the archive: v0.14 dropped the "logs?" step)
         import: pick a .arenaagents -> upload (XHR: the fetch client has no upload progress) + check -> summary
                 -> password / skip the keys -> "整体替换" warning -> progress -> page reload
     - the "old data found" notice bar (#legacy-notice): 查看说明 / 忽略 (F24)

   Server API (docs/archive.md, contract §11.9):
     POST   api/archive/export {include_keys, password?, include_logs} -> {job_id}
     GET    api/archive/jobs/{id}       GET api/archive/status      GET api/archive/exports/{name} (download)
     POST   api/archive/import (the file is the body; ?name=) -> {import_id, name, size_bytes, summary, expires_in}
     POST   api/archive/import/{id}/apply {password?, skip_keys} -> {job_id}     DELETE api/archive/import/{id}
     POST   api/notices/legacy-data:dismiss
   SSE: archive.progress {job_id, kind, phase, done, total, status, message?, code?, result?}

   ctx: { api, Core, showToast, getSession(), getToken(), buildUrl(path), requestToken(info), reload(), openDialog(o),
          wrapHome?(columnOneHtml) -> the HTML of the whole home view (settings.js), onHomeRendered?() (the columns fill themselves),
          onOpened?() (the dialog was opened),
          document?, window?, XMLHttpRequest? }
   ========================================================================== */

(function (root) {
  "use strict";

  // ---------------------------------------------------------------------------
  // the password rule: the same function as app/archive/kdf.py (password_problem), checked against one shared table
  // (tests/archive/password_cases.json) by both test suites
  // ---------------------------------------------------------------------------
  const MIN_PASSWORD_CHARS = 12;
  const MAX_PASSWORD_CHARS = 256;
  const MIN_PASSWORD_CLASSES = 3;
  const RULE_TEXT = `至少 ${MIN_PASSWORD_CHARS} 位，并且至少包含大写字母、小写字母、数字、符号中的三类`;
  const CLASS_LABELS = { upper: "大写", lower: "小写", digit: "数字", symbol: "符号" };

  function passwordChars(password) {
    return Array.from(String(password).normalize("NFC"));
  }

  /** Which of upper / lower / digit / symbol a password uses (every character that is not A-Z a-z 0-9 is a symbol). */
  function passwordClasses(password) {
    const present = new Set();
    for (const ch of passwordChars(password)) {
      if (ch >= "A" && ch <= "Z") present.add("upper");
      else if (ch >= "a" && ch <= "z") present.add("lower");
      else if (ch >= "0" && ch <= "9") present.add("digit");
      else present.add("symbol");
    }
    return ["upper", "lower", "digit", "symbol"].filter((name) => present.has(name));
  }

  /** The reason a new password is refused (Chinese), or null when it is fine. */
  function passwordProblem(password) {
    if (typeof password !== "string") return "请输入密码。";
    const chars = passwordChars(password);
    if (chars.some((ch) => ch.codePointAt(0) < 0x20 || (ch.codePointAt(0) >= 0x7f && ch.codePointAt(0) <= 0x9f))) {
      return "密码不能包含控制字符。";
    }
    if (chars.length > MAX_PASSWORD_CHARS) return `密码最长 ${MAX_PASSWORD_CHARS} 位。`;
    if (chars.length < MIN_PASSWORD_CHARS) return `密码至少需要 ${MIN_PASSWORD_CHARS} 位（现在 ${chars.length} 位）。`;
    const classes = passwordClasses(password);
    if (classes.length < MIN_PASSWORD_CLASSES) {
      return `密码需要包含大写字母、小写字母、数字、符号中的至少 ${MIN_PASSWORD_CLASSES} 类（现在 ${classes.length} 类）。`;
    }
    return null;
  }

  // ---------------------------------------------------------------------------
  // pure helpers
  // ---------------------------------------------------------------------------
  const EXPORT_LABELS = {
    flush: "正在保存当前对局…",
    collect: "正在收集文件…",
    encrypt: "正在加密 API Key（需要几秒钟）…",
    pack: "正在打包…",
    finalize: "正在写入文件…",
  };
  const IMPORT_LABELS = {
    verify: "正在校验并解包…",
    validate: "正在检查内容…",
    backup: "正在备份当前数据…",
    replace: "正在替换数据…",
    keys: "正在保存 API Key…",
    reload: "正在重新载入…",
    finalize: "正在清理…",
  };
  // where a phase sits on the overall bar (percent): [from, to]
  const EXPORT_SPANS = { flush: [0, 5], collect: [5, 10], encrypt: [10, 25], pack: [25, 95], finalize: [95, 100] };
  const IMPORT_SPANS = {
    verify: [0, 60],
    validate: [60, 65],
    backup: [65, 70],
    replace: [70, 85],
    keys: [85, 90],
    reload: [90, 98],
    finalize: [98, 100],
  };

  function formatBytes(value) {
    const n = Number(value);
    if (!Number.isFinite(n) || n < 0) return "—";
    if (n < 1024) return `${Math.round(n)} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
    return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
  }

  /** Overall percent (0-100) of a job from its phase and the phase's own done / total. */
  function overallPercent(kind, phase, done, total) {
    const spans = kind === "import" ? IMPORT_SPANS : EXPORT_SPANS;
    const span = spans[phase];
    if (!span) return 0;
    const fraction = total > 0 ? Math.min(1, Math.max(0, done / total)) : 0;
    return Math.round(span[0] + (span[1] - span[0]) * fraction);
  }

  function phaseLabel(kind, phase) {
    return (kind === "import" ? IMPORT_LABELS : EXPORT_LABELS)[phase] || "处理中…";
  }

  /** The phase a job is in before its first event arrives. */
  const startPhase = (kind) => (kind === "import" ? "verify" : "flush");

  function create(ctx) {
    const opts = ctx || {};
    const api = opts.api;
    const Core = opts.Core || root.ArenaCore;
    const doc = opts.document || root.document;
    const win = opts.window || root;
    const showToast = opts.showToast || (() => {});
    const getSession = opts.getSession || (() => null);
    const getToken = opts.getToken || (() => "");
    const buildUrl = opts.buildUrl || ((path) => path);
    const requestToken = opts.requestToken || (async () => null);
    const openDialog = opts.openDialog || null;
    const wrapHome = typeof opts.wrapHome === "function" ? opts.wrapHome : null; // v0.14: settings.js puts the home view into its five columns
    const onHomeRendered = typeof opts.onHomeRendered === "function" ? opts.onHomeRendered : null;
    const onOpened = typeof opts.onOpened === "function" ? opts.onOpened : null; // v0.14: settings.js reads what its columns show again
    const doReload =
      opts.reload ||
      (() => {
        try {
          win.location.reload();
        } catch {
          /* nowhere to reload in a test DOM */
        }
      });
    if (!Core || !doc || !api) return null;

    const esc = Core.escapeHtml;
    const enc = encodeURIComponent;
    const $ = (id) => (typeof doc.getElementById === "function" ? doc.getElementById(id) : null);
    const el = {
      btnOpen: $("btn-open-settings-modal"),
      modal: $("settings-modal"),
      btnClose: $("btn-close-settings-modal"),
      title: $("settings-title"),
      body: $("settings-body"),
      footer: $("settings-footer"),
      fileInput: $("file-archive-import"),
      legacy: $("legacy-notice"),
      legacyText: $("legacy-notice-text"),
      btnLegacyHelp: $("btn-legacy-help"),
      btnLegacyDismiss: $("btn-legacy-dismiss"),
    };

    const POLL_MS = 1500;
    const RELOAD_DELAY_MS = 1500;

    const fresh = {
      export: () => ({
        includeKeys: false,
        password: "",
        confirm: "",
        starting: false,
        finished: false,
        early: [], // events that arrive before the HTTP answer names the job
        jobId: "",
        kind: "export",
        phase: "",
        done: 0,
        total: 0,
        result: null,
        error: "",
        errorCode: "",
        passwordError: "",
        timer: null,
      }),
      import: () => ({
        file: null,
        name: "",
        size: 0,
        uploading: false,
        uploadSent: 0,
        uploadVerifying: false,
        verifyDone: 0,
        verifyTotal: 0,
        xhr: null,
        importId: "",
        summary: null,
        password: "",
        passwordError: "",
        applying: false,
        starting: false,
        finished: false,
        early: [],
        jobId: "",
        phase: "",
        done: 0,
        total: 0,
        result: null,
        error: "",
        errorCode: "",
        timer: null,
        reloadTimer: null,
      }),
    };
    const st = {
      view: "home",
      export: fresh.export(),
      import: fresh.import(),
      pending: null, // GET /api/archive/status: an uploaded archive waiting for a decision (e.g. after a page reload)
      serverBusy: false,
      legacy: { found: false, dismissed: false, path: "" },
    };

    // -------------------------------------------------------------------------
    // small DOM helpers
    // -------------------------------------------------------------------------
    function on(node, type, handler) {
      if (node && typeof node.addEventListener === "function") node.addEventListener(type, handler);
    }

    function closestFrom(target, selector) {
      let node = target;
      if (node && typeof node.closest !== "function" && node.parentElement) node = node.parentElement;
      return node && typeof node.closest === "function" ? node.closest(selector) : null;
    }

    const modalOpen = () => Boolean(el.modal && el.modal.style && el.modal.style.display === "flex");
    const isRunning = () => {
      const s = getSession();
      return Boolean(s && s.status === "running");
    };
    const errorText = (err) => (err && (err.detail || err.message)) || "操作失败";

    function button(label, action, { cls = "btn-default", disabled = false, id = "", title = "" } = {}) {
      return `<button type="button" class="btn ${cls}"${id ? ` id="${id}"` : ""} data-action="${action}"${disabled ? " disabled" : ""}${
        title ? ` title="${esc(title)}"` : ""
      }>${esc(label)}</button>`;
    }

    // -------------------------------------------------------------------------
    // views: each returns {title, body, footer} (HTML strings; every dynamic value is escaped)
    // -------------------------------------------------------------------------
    /** The progress block of a job.  It is ALWAYS drawn in full (also while the job is still starting): the events only
     *  patch the label, the bar and the percentage in place (updateProgressDom), they never create them. */
    function barHtml(kind, phase, done, total, label) {
      const percent = overallPercent(kind, phase, done, total);
      const bytes = (phase === "pack" || phase === "verify") && total > 0 ? ` · ${formatBytes(done)} / ${formatBytes(total)}` : "";
      return (
        `<div class="archive-phase" id="archive-phase">${esc(label || phaseLabel(kind, phase))}</div>` +
        `<div class="archive-bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${percent}">` +
        `<div class="archive-bar-fill" id="archive-bar-fill" style="width:${percent}%"></div></div>` +
        `<div class="archive-percent form-hint" id="archive-percent">${percent}%${esc(bytes)}</div>`
      );
    }

    function homeView() {
      const running = isRunning();
      const busy = st.serverBusy;
      const pending = st.pending;
      const pendingBlock = pending
        ? `<div class="archive-pending" id="archive-pending">有一份已上传并校验过的存档等待你决定：<strong>${esc(pending.name || "存档")}</strong>（${esc(
            formatBytes(pending.size_bytes)
          )}）。${button("继续导入", "pending-continue", { cls: "btn-xs" })} ${button("丢弃", "pending-discard", { cls: "btn-xs" })}</div>`
        : "";
      const busyNote = busy ? `<div class="form-hint archive-busy" id="archive-busy">另一个导出 / 导入任务正在进行，请等它结束。</div>` : "";
      const columnOne =
          `<div class="settings-section" id="settings-export">` +
          `<div class="settings-section-title">导出存档</div>` +
          `<p class="form-hint">把数据目录里的全部内容（设置、Provider、价格表、官方契约、规则与学习材料、全部 bot 和它们的记忆与待确认提案、对局和全部日志；API Key 可选）打包成一个 <code>.arenaagents</code> 文件，用来备份或搬到另一台电脑。` +
          `对局进行中也可以导出。</p>` +
          button("导出存档…", "export-start", { cls: "btn-primary-outline", id: "btn-archive-export", disabled: busy }) +
          `</div>` +
          `<div class="settings-section" id="settings-import">` +
          `<div class="settings-section-title">导入存档</div>` +
          `<p class="form-hint">用存档里的内容<strong>整体替换</strong>当前数据。替换前会先把当前数据备份到 <code>data/backups/</code>（保留最近 3 份），失败会自动恢复。</p>` +
          button("导入存档…", "import-pick", { cls: "btn-primary-outline", id: "btn-archive-import", disabled: running || busy }) +
          (running ? `<div class="form-hint archive-running" id="archive-running-hint">对局正在运行：请先点击「暂停」，再导入存档。</div>` : "") +
          pendingBlock +
          `</div>` +
          busyNote;
      return {
        title: "设置",
        body: wrapHome ? wrapHome(columnOne) : columnOne,
        footer: button("关闭", "close", { id: "btn-archive-close" }),
      };
    }

    // ---- export -------------------------------------------------------------
    function exportKeysView() {
      return {
        title: "导出存档 · 1/2 API Key",
        body:
          `<p>要把 <strong>API Key</strong> 一起导出吗？</p>` +
          `<p class="form-hint">选「导出」后，Key 会用你下一步设置的密码加密再放进存档（AES-256-GCM）；忘记密码就无法恢复 Key，但其余内容不受影响。` +
          `选「不导出」则存档里没有任何 Key，导入后需要在 Provider 里重新填写。</p>`,
        footer:
          button("取消", "back-home") +
          button("不导出 Key", "export-keys-no", { id: "btn-export-keys-no" }) +
          button("导出 Key（需要密码）", "export-keys-yes", { cls: "btn-primary-outline", id: "btn-export-keys-yes" }),
      };
    }

    function exportPasswordView() {
      const e = st.export;
      return {
        title: "导出存档 · 2/2 设置密码",
        body:
          `<p>设置存档密码。导入存档时需要它才能解开 API Key。</p>` +
          `<div class="form-group"><label class="form-label" for="input-archive-password">密码</label>` +
          `<input type="password" class="form-input" id="input-archive-password" autocomplete="new-password" value="${esc(e.password)}" /></div>` +
          `<div class="form-group"><label class="form-label" for="input-archive-password2">再输入一遍</label>` +
          `<input type="password" class="form-input" id="input-archive-password2" autocomplete="new-password" value="${esc(e.confirm)}" /></div>` +
          `<div class="archive-rules" id="archive-pw-rules"></div>` +
          `<div class="form-error" id="archive-pw-error" ${e.passwordError ? "" : "hidden"}>${esc(e.passwordError)}</div>`,
        footer:
          button("上一步", "export-to-keys") + button("开始导出", "export-run", { cls: "btn-primary-outline", id: "btn-archive-next", disabled: true }),
      };
    }

    function exportProgressView() {
      const e = st.export;
      return {
        title: "正在导出存档",
        body:
          barHtml("export", e.phase || startPhase("export"), e.done, e.total, e.starting && !e.jobId ? "正在启动…" : "") +
          `<p class="form-hint">导出在服务器后台进行，关闭这个窗口不会中断；完成后会自动弹出结果。</p>`,
        footer: button("关闭窗口", "close", { id: "btn-archive-close" }),
      };
    }

    /** What an archive holds, in one line.  v2 (v0.14): bots, the contract, the rules; an older server's roster_seats is still understood. */
    function contentsLine(result) {
      const c = (result && result.contents) || {};
      const parts = [`${c.games || 0} 局对局`];
      if (c.bots !== undefined || c.roster_seats === undefined) parts.push(`${c.bots || 0} 个 bot`);
      else parts.push(`${c.roster_seats || 0} 个座位（旧版名册）`);
      parts.push(`${c.providers || 0} 个 Provider`);
      if (c.contract !== undefined) parts.push(c.contract ? "含官方契约" : "不含官方契约");
      if (c.rules !== undefined) parts.push(`${c.rules || 0} 个剧本`);
      parts.push(c.has_secrets ? "含 API Key（已加密）" : "不含 API Key");
      parts.push(c.has_logs ? "含思考日志" : "不含思考日志");
      return parts.join(" · ");
    }

    function warningsHtml(list) {
      const items = Array.isArray(list) ? list.filter(Boolean) : [];
      if (!items.length) return "";
      return `<ul class="archive-warnings" id="archive-warnings">${items.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>`;
    }

    function exportDoneView() {
      const r = st.export.result || {};
      const where = r.path || r.display_path || r.name || "";
      return {
        title: "导出完成",
        body:
          `<p class="archive-ok">✅ 存档已生成。</p>` +
          `<div class="archive-result"><div>保存位置：<code id="archive-saved-path">${esc(where)}</code></div>` +
          (r.download_name ? `<div>下载时的文件名：<code id="archive-download-name">${esc(r.download_name)}</code></div>` : "") +
          `<div>大小：${esc(formatBytes(r.size_bytes))} · ${esc(contentsLine(r))}</div></div>` +
          `<p class="form-hint">文件保存在运行 Arena-Agents 的电脑上（<code>data/exports/</code>）。点「下载」可以把它保存到你正在用的这台电脑。</p>` +
          warningsHtml(r.warnings),
        footer: button("下载", "export-download", { cls: "btn-primary-outline", id: "btn-archive-download" }) + button("关闭", "close", { id: "btn-archive-close" }),
      };
    }

    function errorView(title, message, again) {
      return {
        title,
        body: `<p class="archive-fail">⚠ ${esc(message)}</p>`,
        footer: button(again.label, again.action, { cls: "btn-primary-outline", id: "btn-archive-again" }) + button("关闭", "close", { id: "btn-archive-close" }),
      };
    }

    // ---- import -------------------------------------------------------------
    function importPickView() {
      return {
        title: "导入存档",
        body:
          `<p>选择一个 <code>.arenaagents</code> 存档文件。</p>` +
          `<p class="archive-replace-warning" id="archive-replace-warning">⚠ 导入会<strong>整体替换</strong>当前数据；旧数据会先备份到 <code>data/backups/</code>。</p>` +
          (isRunning() ? `<p class="form-error" id="archive-running-hint">对局正在运行：请先点击「暂停」，再导入存档。</p>` : ""),
        footer: button("取消", "back-home") + button("选择文件…", "import-choose", { cls: "btn-primary-outline", id: "btn-archive-choose", disabled: isRunning() }),
      };
    }

    function importUploadView() {
      const i = st.import;
      if (i.uploadVerifying) {
        const percent = i.verifyTotal > 0 ? Math.round((100 * i.verifyDone) / i.verifyTotal) : 0;
        return {
          title: "正在校验存档",
          body:
            `<div class="archive-phase" id="archive-phase">服务器正在校验存档（格式、清单、每个文件的 SHA-256、每局对局）…</div>` +
            `<div class="archive-bar"><div class="archive-bar-fill" id="archive-bar-fill" style="width:${percent}%"></div></div>` +
            `<div class="archive-percent form-hint" id="archive-percent">${percent}%</div>`,
          footer: button("取消", "import-cancel", { id: "btn-archive-cancel" }),
        };
      }
      const percent = i.size > 0 ? Math.min(100, Math.round((100 * i.uploadSent) / i.size)) : 0;
      return {
        title: "正在上传存档",
        body:
          `<div class="archive-phase" id="archive-phase">${esc(i.name)}</div>` +
          `<div class="archive-bar"><div class="archive-bar-fill" id="archive-bar-fill" style="width:${percent}%"></div></div>` +
          `<div class="archive-percent form-hint" id="archive-percent">${percent}% · ${esc(formatBytes(i.uploadSent))} / ${esc(formatBytes(i.size))}</div>`,
        footer: button("取消", "import-cancel", { id: "btn-archive-cancel" }),
      };
    }

    function factRow(label, value, id) {
      return `<dt>${esc(label)}</dt><dd${id ? ` id="${id}"` : ""}>${value}</dd>`;
    }

    function importSummaryView() {
      const i = st.import;
      const s = i.summary || {};
      const names = Array.isArray(s.provider_names) && s.provider_names.length ? `（${s.provider_names.map(esc).join("、")}）` : "";
      const facts =
        factRow("文件", `${esc(i.name)} · ${esc(formatBytes(i.size))}`, "archive-fact-file") +
        factRow("创建时间", esc(Core.formatDateTime ? Core.formatDateTime(s.created_at) || s.created_at : s.created_at), "archive-fact-created") +
        factRow("应用版本", esc(s.app_version || "—"), "archive-fact-version") +
        factRow("对局", `${esc(String(s.games ?? 0))} 局`, "archive-fact-games") +
        factRow("Provider", `${esc(String(s.providers ?? 0))} 个${names}`, "archive-fact-providers") +
        (s.roster_seats !== undefined && s.bots === undefined
          ? factRow("名册", `${esc(String(s.roster_seats ?? 0))} 个座位（旧格式：导入时迁移成 bot）`, "archive-fact-roster")
          : factRow("玩家（bot）", `${esc(String(s.bots ?? 0))} 个`, "archive-fact-bots") +
            factRow("官方契约", s.contract ? "含" : "不含", "archive-fact-contract") +
            factRow("规则", `${esc(String(s.rules ?? 0))} 个剧本`, "archive-fact-rules")) +
        factRow("API Key", s.has_secrets ? "含（用密码加密）" : "不含", "archive-fact-secrets") +
        factRow("思考日志", s.has_logs ? "含" : "不含", "archive-fact-logs");
      const running = isRunning();
      const passwordBlock = s.has_secrets
        ? `<div class="form-group"><label class="form-label" for="input-archive-import-password">存档密码（用来解开 API Key）</label>` +
          `<input type="password" class="form-input" id="input-archive-import-password" autocomplete="off" value="${esc(i.password)}" />` +
          `<div class="form-error" id="archive-import-password-error" ${i.passwordError ? "" : "hidden"}>${esc(i.passwordError)}</div>` +
          `<div class="form-hint">没有密码、或者不想导入 Key：选「跳过 API Key」。本机已有的、和存档里 Provider 同 id 的 Key 会保留；其余 Provider 导入后会显示「未设置 API Key」。</div></div>`
        : "";
      const footer =
        button("取消", "import-cancel", { id: "btn-archive-cancel" }) +
        (s.has_secrets
          ? button("跳过 API Key 并导入", "import-apply-skip", { id: "btn-archive-apply-skip", disabled: running }) +
            button("整体替换并导入", "import-apply", { cls: "btn-danger-outline", id: "btn-archive-apply", disabled: running })
          : button("整体替换并导入", "import-apply-skip", { cls: "btn-danger-outline", id: "btn-archive-apply", disabled: running }));
      return {
        title: "导入存档 · 确认",
        body:
          `<dl class="archive-facts" id="archive-facts">${facts}</dl>` +
          warningsHtml(s.warnings) +
          `<p class="archive-replace-warning" id="archive-replace-warning">⚠ 将<strong>整体替换当前数据</strong>（对局、bot 与它们的记忆、官方契约与规则、Provider、设置、日志）。旧数据会先备份到 ` +
          `<code>data/backups/</code>（保留最近 3 份），导入失败会自动恢复。</p>` +
          passwordBlock +
          (running ? `<p class="form-error" id="archive-running-hint">对局正在运行：请先点击「暂停」，再导入存档。</p>` : ""),
        footer,
      };
    }

    function importProgressView() {
      const i = st.import;
      return {
        title: "正在导入存档",
        body:
          barHtml("import", i.phase || startPhase("import"), i.done, i.total, i.starting && !i.jobId ? "正在启动…" : "") +
          `<p class="form-hint">请不要关闭或刷新页面；完成后页面会自动刷新。</p>`,
        footer: "",
      };
    }

    function importDoneView() {
      const r = st.import.result || {};
      const lines = [`${r.games || 0} 局对局`, `${r.providers || 0} 个 Provider`];
      if (r.keys_imported) lines.push(`${r.keys_imported} 个 API Key 已导入`);
      if (r.keys_kept) lines.push(`${r.keys_kept} 个本机 Key 已保留`);
      const missing = Array.isArray(r.providers_without_key) ? r.providers_without_key : [];
      return {
        title: "导入完成",
        body:
          `<p class="archive-ok">✅ 导入完成：${esc(lines.join(" · "))}。</p>` +
          (missing.length ? `<p class="form-hint archive-missing-keys" id="archive-missing-keys">这些 Provider 还没有 API Key，开始对局前请在 Provider 里填写：${missing.map(esc).join("、")}</p>` : "") +
          (r.backup ? `<p class="form-hint">原来的数据已备份在 <code>data/backups/${esc(r.backup)}/</code>。</p>` : "") +
          warningsHtml(r.warnings) +
          `<p class="form-hint">页面即将自动刷新…</p>`,
        footer: button("立即刷新", "reload", { cls: "btn-primary-outline", id: "btn-archive-reload" }),
      };
    }

    function renderCurrent() {
      switch (st.view) {
        case "export-keys":
          return exportKeysView();
        case "export-password":
          return exportPasswordView();
        case "export-progress":
          return exportProgressView();
        case "export-done":
          return exportDoneView();
        case "export-error":
          return errorView("导出失败", st.export.error || "导出失败", { label: "重新开始", action: "export-start" });
        case "import-pick":
          return importPickView();
        case "import-upload":
          return importUploadView();
        case "import-summary":
          return importSummaryView();
        case "import-progress":
          return importProgressView();
        case "import-done":
          return importDoneView();
        case "import-error":
          return errorView("导入失败", st.import.error || "导入失败", { label: "重新选择文件", action: "import-pick" });
        default:
          return homeView();
      }
    }

    function render() {
      if (!el.body) return;
      const view = renderCurrent();
      if (el.title) el.title.textContent = view.title;
      el.body.innerHTML = view.body;
      if (el.footer) el.footer.innerHTML = view.footer;
      if (st.view === "export-password") updatePasswordState();
      if (st.view === "home" && onHomeRendered) onHomeRendered();
    }

    function setView(view) {
      st.view = view;
      render();
    }

    // -------------------------------------------------------------------------
    // the live password rule (export step 2)
    // -------------------------------------------------------------------------
    function ruleItem(ok, text) {
      return `<li class="${ok ? "ok" : "bad"}">${ok ? "✓" : "✗"} ${esc(text)}</li>`;
    }

    function updatePasswordState() {
      const e = st.export;
      const chars = passwordChars(e.password).length;
      const classes = passwordClasses(e.password);
      const problem = passwordProblem(e.password);
      const matches = e.password.length > 0 && e.password === e.confirm;
      const rules = $("archive-pw-rules");
      if (rules) {
        const classText = ["upper", "lower", "digit", "symbol"].map((name) => `${CLASS_LABELS[name]}${classes.includes(name) ? "✓" : "✗"}`).join(" ");
        rules.innerHTML =
          `<ul class="archive-rule-list">` +
          ruleItem(chars >= MIN_PASSWORD_CHARS && chars <= MAX_PASSWORD_CHARS, `至少 ${MIN_PASSWORD_CHARS} 位（现在 ${chars} 位）`) +
          ruleItem(classes.length >= MIN_PASSWORD_CLASSES, `大写 / 小写 / 数字 / 符号中至少三类：${classText}`) +
          ruleItem(matches, "两次输入一致") +
          `</ul>`;
      }
      const next = $("btn-archive-next");
      if (next) next.disabled = !(problem === null && matches);
      return { problem, matches };
    }

    function onInput(evt) {
      const target = evt && evt.target;
      const id = target && target.id;
      const value = target && target.value !== undefined ? String(target.value) : "";
      if (id === "input-archive-password") {
        st.export.password = value;
        st.export.passwordError = "";
        updatePasswordState();
      } else if (id === "input-archive-password2") {
        st.export.confirm = value;
        updatePasswordState();
      } else if (id === "input-archive-import-password") {
        st.import.password = value;
        if (st.import.passwordError) {
          st.import.passwordError = "";
          const box = $("archive-import-password-error");
          if (box) {
            box.hidden = true;
            box.textContent = "";
          }
        }
      }
    }

    // -------------------------------------------------------------------------
    // jobs: progress events (SSE) with a polling fallback
    // -------------------------------------------------------------------------
    function stopTimer(job) {
      if (job.timer !== null && typeof win.clearTimeout === "function") win.clearTimeout(job.timer);
      job.timer = null;
    }

    function schedulePoll(kind) {
      const job = st[kind];
      stopTimer(job);
      if (typeof win.setTimeout !== "function") return;
      job.timer = win.setTimeout(() => {
        job.timer = null;
        pollJob(kind);
      }, POLL_MS);
    }

    async function pollJob(kind) {
      const job = st[kind];
      if (!job.jobId) return;
      try {
        const body = await api.get(`api/archive/jobs/${enc(job.jobId)}`);
        if (body && body.job_id === job.jobId) applyJobState(kind, body);
      } catch {
        /* the next tick (or the SSE stream) tries again */
      }
      const current = st[kind];
      if (current.jobId === job.jobId && !current.finished && ((kind === "export" && st.view === "export-progress") || (kind === "import" && st.view === "import-progress"))) {
        schedulePoll(kind);
      }
    }

    /** One job state (an SSE event or GET /jobs/{id}): update the numbers; finish the wizard on done / error. */
    function applyJobState(kind, data) {
      const job = st[kind];
      if (job.finished) return; // the SSE event and the poll both report the end: show it once
      if (data.status === "running") {
        job.phase = data.phase || job.phase;
        job.done = Number(data.done) || 0;
        job.total = Number(data.total) || 0;
        if (kind === "export" ? st.view === "export-progress" : st.view === "import-progress") updateProgressDom(kind);
        return;
      }
      stopTimer(job);
      job.finished = true;
      if (data.status === "error") {
        job.error = data.message || "操作失败";
        job.errorCode = data.code || "";
        job.starting = false;
        job.applying = false;
        if (kind === "export") {
          if (!modalOpen()) showModal();
          setView("export-error");
        } else {
          if (!modalOpen()) showModal();
          setView("import-error");
        }
        return;
      }
      // done
      job.starting = false;
      if (kind === "export") {
        finishExport(data);
      } else {
        finishImport(data);
      }
    }

    function updateProgressDom(kind) {
      const job = st[kind];
      const phase = job.phase || startPhase(kind);
      const percent = overallPercent(kind, phase, job.done, job.total);
      const fill = $("archive-bar-fill");
      if (!fill) {
        render(); // the view on screen was drawn without a bar (it should not be): draw it in full instead of patching nothing
        return;
      }
      if (fill.style) fill.style.width = `${percent}%`;
      const label = $("archive-phase");
      if (label) label.textContent = phaseLabel(kind, phase);
      const text = $("archive-percent");
      if (text) {
        const bytes = (phase === "pack" || phase === "verify") && job.total > 0 ? ` · ${formatBytes(job.done)} / ${formatBytes(job.total)}` : "";
        text.textContent = `${percent}%${bytes}`;
      }
    }

    async function finishExport(data) {
      const job = st.export;
      let result = data.result || null;
      try {
        // the event carries no absolute server path; this request (loopback clients only) does
        const full = await api.get(`api/archive/jobs/${enc(job.jobId)}`);
        if (full && full.result) result = full.result;
      } catch {
        /* keep the event's result */
      }
      job.result = result || {};
      if (!modalOpen()) showModal();
      setView("export-done");
      showToast("存档已导出");
    }

    function finishImport(data) {
      const job = st.import;
      job.result = data.result || {};
      job.applying = false;
      if (!modalOpen()) showModal();
      setView("import-done");
      scheduleReload();
    }

    function scheduleReload() {
      const job = st.import;
      if (job.reloadTimer !== null) return;
      if (typeof win.setTimeout !== "function") {
        doReload();
        return;
      }
      job.reloadTimer = win.setTimeout(() => {
        job.reloadTimer = null;
        doReload();
      }, RELOAD_DELAY_MS);
    }

    /** An `archive.progress` SSE event (already normalized by store.js). */
    function onEvent(ev) {
      if (!ev || typeof ev !== "object") return;
      if (ev.kind === "export") {
        const job = st.export;
        if (job.jobId && job.jobId === ev.job_id) {
          applyJobState("export", ev);
          return;
        }
        if (!job.jobId && job.starting) {
          job.early.push(ev); // it can beat the HTTP answer: kept until that answer says which job is ours
          return;
        }
        st.serverBusy = ev.status === "running"; // somebody else's export (another tab)
        if (st.view === "home" && modalOpen()) render();
        return;
      }
      if (ev.kind !== "import") return;
      const job = st.import;
      if (ev.phase === "verify" && ev.status === "running" && st.view === "import-upload" && job.uploadVerifying && !job.jobId) {
        job.verifyDone = ev.done;
        job.verifyTotal = ev.total;
        const fill = $("archive-bar-fill");
        const percent = ev.total > 0 ? Math.round((100 * ev.done) / ev.total) : 0;
        if (fill && fill.style) fill.style.width = `${percent}%`;
        const text = $("archive-percent");
        if (text) text.textContent = `${percent}%`;
        return;
      }
      if (job.jobId && job.jobId === ev.job_id) {
        applyJobState("import", ev);
        return;
      }
      if (!job.jobId && job.starting) {
        job.early.push(ev);
        return;
      }
      if (ev.status === "done") {
        // another browser tab replaced the data: this page shows the old state, so start over
        doReload();
      } else {
        st.serverBusy = ev.status === "running";
      }
    }

    // -------------------------------------------------------------------------
    // export actions
    // -------------------------------------------------------------------------
    function startExportFlow() {
      stopTimer(st.export);
      st.export = fresh.export();
      setView("export-keys");
    }

    async function runExport() {
      const e = st.export;
      e.starting = true;
      e.error = "";
      setView("export-progress");
      const body = { include_keys: e.includeKeys, include_logs: true }; // (v2 servers ignore include_logs: the logs are always in)
      if (e.includeKeys) body.password = e.password;
      try {
        const res = await api.post("api/archive/export", body);
        e.jobId = res.job_id;
        e.starting = false;
        if (st.view === "export-progress") updateProgressDom("export"); // "正在启动…" becomes the phase it really is in
        for (const ev of e.early.splice(0)) if (ev.job_id === e.jobId) applyJobState("export", ev);
        // the poll catches anything the stream missed
        if (st.view === "export-progress" && !e.finished) schedulePoll("export");
      } catch (err) {
        e.starting = false;
        if (err && err.code === "weak_password") {
          e.passwordError = errorText(err);
          setView("export-password");
          return;
        }
        e.error = errorText(err);
        e.errorCode = (err && err.code) || "";
        setView("export-error");
      }
    }

    function downloadExport() {
      const r = st.export.result;
      if (!r || !r.name) return;
      const url = buildUrl(`api/archive/exports/${enc(r.name)}`, { token: true });
      try {
        const a = doc.createElement("a");
        a.href = url;
        a.download = r.download_name || r.name; // (H42: the name the user keeps; the URL has the fixed internal one)
        if (doc.body && typeof doc.body.appendChild === "function") doc.body.appendChild(a);
        a.click();
        if (typeof a.remove === "function") a.remove();
      } catch {
        showToast("无法开始下载，请直接打开 data/exports/ 里的文件");
      }
    }

    // -------------------------------------------------------------------------
    // import actions
    // -------------------------------------------------------------------------
    function discardPending() {
      const id = st.import.importId || (st.pending && st.pending.import_id);
      if (id) api.del(`api/archive/import/${enc(id)}`).catch(() => {});
    }

    function resetImport({ discard = false } = {}) {
      const job = st.import;
      if (job.xhr && typeof job.xhr.abort === "function") {
        try {
          job.xhr.abort();
        } catch {
          /* already finished */
        }
      }
      if (discard && job.importId) api.del(`api/archive/import/${enc(job.importId)}`).catch(() => {});
      stopTimer(job);
      if (job.reloadTimer !== null && typeof win.clearTimeout === "function") win.clearTimeout(job.reloadTimer);
      st.import = fresh.import();
    }

    function chooseFile() {
      if (isRunning()) {
        showToast("对局正在运行：请先暂停，再导入存档");
        return;
      }
      if (el.fileInput && typeof el.fileInput.click === "function") {
        el.fileInput.value = "";
        el.fileInput.click();
      }
    }

    function onFilePicked(evt) {
      const file = evt && evt.target && evt.target.files && evt.target.files[0];
      if (!file) return;
      if (el.fileInput) el.fileInput.value = "";
      startUpload(file);
    }

    function parseXhrError(xhr) {
      let detail = "";
      let code = "";
      try {
        const body = JSON.parse(xhr.responseText || "{}");
        detail = Core.formatDetail(body && body.detail !== undefined ? body.detail : body);
        code = body && typeof body.code === "string" ? body.code : "";
      } catch {
        detail = "";
      }
      return { status: xhr.status, detail: detail || `服务器返回了 ${xhr.status}`, code };
    }

    function startUpload(file, retried = false) {
      const XHR = opts.XMLHttpRequest || root.XMLHttpRequest;
      resetImport({ discard: true });
      const i = st.import;
      i.file = file;
      i.name = String(file.name || "archive.arenaagents");
      i.size = Number(file.size) || 0;
      i.uploading = true;
      setView("import-upload");
      if (typeof XHR !== "function") {
        failImport("这个浏览器不支持带进度的上传。");
        return;
      }
      const xhr = new XHR();
      i.xhr = xhr;
      xhr.open("POST", buildUrl(`api/archive/import?name=${enc(i.name)}`));
      xhr.setRequestHeader("Content-Type", "application/octet-stream");
      const token = getToken();
      if (token) xhr.setRequestHeader("X-Arena-Token", token);
      if (xhr.upload) {
        xhr.upload.onprogress = (e) => {
          i.uploadSent = Number(e && e.loaded) || 0;
          if (e && e.total) i.size = Number(e.total) || i.size;
          if (st.view === "import-upload" && !i.uploadVerifying) {
            const percent = i.size > 0 ? Math.min(100, Math.round((100 * i.uploadSent) / i.size)) : 0;
            const fill = $("archive-bar-fill");
            if (fill && fill.style) fill.style.width = `${percent}%`;
            const text = $("archive-percent");
            if (text) text.textContent = `${percent}% · ${formatBytes(i.uploadSent)} / ${formatBytes(i.size)}`;
          }
        };
        xhr.upload.onload = () => {
          if (st.import.xhr !== xhr) return;
          i.uploadSent = i.size;
          i.uploadVerifying = true;
          if (st.view === "import-upload") render();
        };
      }
      xhr.onload = () => {
        if (st.import.xhr !== xhr) return;
        i.xhr = null;
        i.uploading = false;
        if (xhr.status === 401 && !retried) {
          requestToken({ hadToken: Boolean(token), usedToken: token }).then((fresh_token) => {
            if (fresh_token) startUpload(file, true);
            else failImport("需要有效的访问令牌。");
          });
          return;
        }
        if (xhr.status >= 200 && xhr.status < 300) {
          let body = null;
          try {
            body = JSON.parse(xhr.responseText);
          } catch {
            body = null;
          }
          if (!body || !body.import_id || !body.summary) {
            failImport("服务器的回答不是预期的格式。");
            return;
          }
          i.importId = body.import_id;
          i.summary = body.summary;
          i.name = body.name || i.name;
          i.size = Number(body.size_bytes) || i.size;
          i.uploadVerifying = false;
          setView("import-summary");
          return;
        }
        const failure = parseXhrError(xhr);
        failImport(failure.detail, failure.code);
      };
      xhr.onerror = () => {
        if (st.import.xhr !== xhr) return;
        i.xhr = null;
        failImport("上传失败：无法连接到服务器。");
      };
      xhr.onabort = () => {
        /* cancelled by the user: the view was already changed */
      };
      xhr.send(file);
    }

    function failImport(message, code) {
      const i = st.import;
      i.uploading = false;
      i.uploadVerifying = false;
      i.applying = false;
      i.starting = false;
      i.error = message;
      i.errorCode = code || "";
      setView("import-error");
    }

    async function applyImport(skipKeys) {
      const i = st.import;
      if (!i.importId || i.applying) return;
      if (isRunning()) {
        showToast("对局正在运行：请先暂停，再导入存档");
        return;
      }
      const hasSecrets = Boolean(i.summary && i.summary.has_secrets);
      if (hasSecrets && !skipKeys && !i.password) {
        i.passwordError = "请输入存档密码，或者选择「跳过 API Key」。";
        const box = $("archive-import-password-error");
        if (box) {
          box.hidden = false;
          box.textContent = i.passwordError;
        }
        return;
      }
      i.applying = true;
      i.starting = true;
      i.passwordError = "";
      const body = { skip_keys: Boolean(skipKeys || !hasSecrets) };
      if (hasSecrets && !skipKeys) body.password = i.password;
      setView("import-progress");
      try {
        const res = await api.post(`api/archive/import/${enc(i.importId)}/apply`, body);
        i.jobId = res.job_id;
        i.starting = false;
        i.password = "";
        if (st.view === "import-progress") updateProgressDom("import");
        for (const ev of i.early.splice(0)) if (ev.job_id === i.jobId) applyJobState("import", ev);
        if (st.view === "import-progress" && !i.finished) schedulePoll("import");
      } catch (err) {
        i.applying = false;
        i.starting = false;
        const code = (err && err.code) || "";
        if (code === "wrong_password" || code === "keys_mismatch") {
          // wrong_password: try again; keys_mismatch: the keys are not trusted (providers.json was changed after they were
          // sealed) - the message says to skip them.  Either way the upload is still on the server: back to the summary.
          i.passwordError = errorText(err);
          setView("import-summary");
          return;
        }
        if (code === "game_running" || code === "busy") {
          showToast(errorText(err));
          setView("import-summary");
          return;
        }
        failImport(errorText(err), code);
      }
    }

    async function continuePending() {
      const p = st.pending;
      if (!p) return;
      resetImport();
      const i = st.import;
      i.importId = p.import_id;
      i.summary = p.summary || {};
      i.name = p.name || "存档";
      i.size = Number(p.size_bytes) || 0;
      st.pending = null;
      setView("import-summary");
    }

    // -------------------------------------------------------------------------
    // the dialog
    // -------------------------------------------------------------------------
    function showModal() {
      if (el.modal) el.modal.style.display = "flex";
    }

    function hideModal() {
      if (el.modal) el.modal.style.display = "none";
    }

    async function refreshStatus() {
      try {
        const status = await api.get("api/archive/status");
        st.serverBusy = Boolean(status && status.busy);
        st.pending = status && status.import ? status.import : null;
        const active = status && status.job;
        if (active && active.status === "running" && !st[active.kind === "import" ? "import" : "export"].jobId) {
          // a job started elsewhere (another tab, before a page reload): follow it
          const kind = active.kind === "import" ? "import" : "export";
          st[kind].jobId = active.job_id;
          st[kind].phase = active.phase;
          st[kind].done = active.done;
          st[kind].total = active.total;
          st[kind].applying = kind === "import";
          setView(kind === "import" ? "import-progress" : "export-progress");
          schedulePoll(kind);
          return;
        }
        if (st.view === "home" && modalOpen()) render();
      } catch {
        /* the dialog works without it */
      }
    }

    function openSettings() {
      showModal();
      if (st.view !== "export-progress" && st.view !== "import-progress" && st.view !== "export-done" && st.view !== "import-done") {
        st.view = "home";
      }
      render();
      if (onOpened) onOpened();
      return refreshStatus();
    }

    /** Back to the home view (the five columns) - unless an import is running or has just finished (that must be seen out). */
    function goHome() {
      if (st.view === "import-progress" || st.view === "import-done" || st.view === "home") return st.view === "home";
      if (st.view === "import-upload" || st.view === "import-summary") resetImport({ discard: true });
      setView("home");
      return true;
    }

    function closeSettings() {
      if (st.view === "import-progress" || st.view === "import-done") {
        showToast(st.view === "import-progress" ? "导入进行中，请等它完成" : "页面即将刷新");
        return false;
      }
      if (st.view === "import-upload" || st.view === "import-summary") resetImport({ discard: true });
      hideModal();
      if (st.view !== "export-progress") st.view = "home";
      return true;
    }

    function onAction(action) {
      switch (action) {
        case "close":
          closeSettings();
          break;
        case "back-home":
          setView("home");
          refreshStatus();
          break;
        case "export-start":
          startExportFlow();
          break;
        case "export-keys-no":
          st.export.includeKeys = false;
          st.export.password = "";
          st.export.confirm = "";
          runExport(); // (v0.14: there is no third step any more - the logs are always in the archive)
          break;
        case "export-keys-yes":
          st.export.includeKeys = true;
          setView("export-password");
          break;
        case "export-to-keys":
          setView("export-keys");
          break;
        case "export-run": {
          // (the password step's own button: only with a password that follows the rule, typed twice)
          if (st.export.includeKeys) {
            const { problem, matches } = updatePasswordState();
            if (!(problem === null && matches)) break;
          }
          runExport();
          break;
        }
        case "export-download":
          downloadExport();
          break;
        case "import-pick":
          resetImport({ discard: true });
          setView("import-pick");
          break;
        case "import-choose":
          chooseFile();
          break;
        case "import-cancel":
          resetImport({ discard: true });
          setView("home");
          refreshStatus();
          break;
        case "import-apply":
          applyImport(false);
          break;
        case "import-apply-skip":
          applyImport(true);
          break;
        case "pending-continue":
          continuePending();
          break;
        case "pending-discard":
          discardPending();
          st.pending = null;
          render();
          break;
        case "reload":
          doReload();
          break;
        default:
          break;
      }
    }

    function onClick(evt) {
      const node = closestFrom(evt && evt.target, "[data-action]");
      if (!node) return;
      if (node.disabled) return;
      onAction(node.getAttribute("data-action"));
    }

    // -------------------------------------------------------------------------
    // "old data found" (F24)
    // -------------------------------------------------------------------------
    function renderLegacy() {
      if (!el.legacy) return;
      el.legacy.hidden = !(st.legacy.found && !st.legacy.dismissed);
    }

    /** `legacy_data` of GET /api/state. */
    function onState(legacy) {
      const d = legacy && typeof legacy === "object" ? legacy : {};
      st.legacy = { found: Boolean(d.found), dismissed: Boolean(d.dismissed), path: typeof d.path === "string" ? d.path : "" };
      renderLegacy();
    }

    function legacyHelpHtml() {
      const where = st.legacy.path
        ? `<p>检测到的旧工作文件夹：<code>${esc(st.legacy.path)}</code>（脚本默认就会找它，不用再指定）。</p>`
        : "";
      return (
        `<p>v0.13 不再使用「工作文件夹」，数据统一放在程序目录的 <code>data/</code> 下。旧版（v0.12.1）的对局、Provider、名册、设置和日志要先转换成存档，再导入：</p>` +
        `<ol class="archive-steps">` +
        `<li>在程序目录打开终端，运行（要用创建旧工作文件夹的<strong>同一个系统用户</strong>，否则解不开旧数据）：` +
        `<pre class="archive-code" id="legacy-command">python tools/migrate_v0121_to_archive.py</pre></li>` +
        `<li>想把 API Key 一起带过来，加上 <code>--include-keys</code>（会让你设置一个密码）；不想要思考日志，加上 <code>--no-logs</code>；旧工作文件夹不在默认位置，用 <code>--workspace &lt;路径&gt;</code> 指定。</li>` +
        `<li>脚本会生成一个 <code>.arenaagents</code> 文件，并列出转换了哪些对局。回到这里，点「⚙ 设置」→「导入存档」，选择它。</li>` +
        `</ol>` +
        where +
        `<p class="form-hint">旧工作文件夹不会被改动。v0.12.0 及更早的对局无法转换，会在脚本的报告里列出。</p>`
      );
    }

    async function showLegacyHelp() {
      if (!openDialog) {
        showToast("请运行：python tools/migrate_v0121_to_archive.py，然后在设置里导入生成的存档");
        return;
      }
      const choice = await openDialog({ title: "导入旧版（v0.12.1）数据", bodyHtml: legacyHelpHtml(), okText: "打开设置", cancelText: "关闭" });
      if (choice && choice.ok) openSettings();
    }

    async function dismissLegacy() {
      try {
        await api.post("api/notices/legacy-data:dismiss");
        st.legacy.dismissed = true;
        renderLegacy();
      } catch (err) {
        showToast(errorText(err));
      }
    }

    // -------------------------------------------------------------------------
    // wiring
    // -------------------------------------------------------------------------
    on(el.btnOpen, "click", () => openSettings());
    on(el.btnClose, "click", () => closeSettings());
    on(el.body, "click", onClick);
    on(el.footer, "click", onClick);
    on(el.body, "input", onInput);
    on(el.body, "change", onInput);
    on(el.fileInput, "change", onFilePicked);
    on(el.btnLegacyHelp, "click", () => showLegacyHelp());
    on(el.btnLegacyDismiss, "click", () => dismissLegacy());
    on(el.modal, "click", (evt) => {
      // a click on the dark backdrop (not on the card) closes the dialog
      if (evt && evt.target === el.modal) closeSettings();
    });
    if (typeof doc.addEventListener === "function") {
      doc.addEventListener("keydown", (evt) => {
        if (evt && evt.key === "Escape" && modalOpen()) closeSettings();
      });
    }
    renderLegacy();

    return {
      openSettings,
      closeSettings,
      goHome,
      isOpen: modalOpen,
      onSession: () => {
        // the game started / stopped: the import entries follow
        if (!modalOpen()) return;
        if (st.view === "home" || st.view === "import-pick" || st.view === "import-summary") render();
      },
      onState,
      onEvent,
      refreshStatus,
      view: () => st.view,
      _state: st,
    };
  }

  root.ArenaArchiveUI = {
    create,
    helpers: {
      passwordProblem,
      passwordClasses,
      formatBytes,
      overallPercent,
      phaseLabel,
      RULE_TEXT,
      MIN_PASSWORD_CHARS,
      MAX_PASSWORD_CHARS,
      MIN_PASSWORD_CLASSES,
    },
  };
})(typeof window !== "undefined" ? window : globalThis);
