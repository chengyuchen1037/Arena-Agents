/* ==========================================================================
   Arena-Agents v0.14 — the five columns of the settings dialog (H35–H40; docs/v0.12-contracts.md §13.7 / §13.9 / §13.10)
   Loaded after history.js and before app.js. Exposes window.ArenaSettingsUI.create(ctx) (plus pure helpers).

   The "⚙ 设置" dialog (#settings-modal) is owned by archive.js (open / close, the export and import wizards).  Its home view is drawn into
   five collapsible columns by this file (archive.js asks for it through ctx.wrapHome / ctx.onHomeRendered):
     ① 总存档     the wizards of archive.js (API key + password rules unchanged; the logs are always in the archive)
     ② 底层设定   the L0 of the twelve bots: clear all / one, per row avatar · name · state · characters · time, expanded: preview, .md out / in, 生成
     ③ 官方契约   source, time, characters, hash, the faults of a contract the server cannot use; .md out / in, back to the default
     ④ 规则       one row per scenario: .md out / in, back to the default; the study materials of each (out, delete, add)
     ⑤ 对局管理   the games: state, winner, time, cost, review x/12, size; expanded: players, review state, files, who was played by a person;
                  delete (with its note), .arenagame out / in (an id that exists: copy / overwrite / cancel), a way to the history dialog and back
   Which columns are open is kept in localStorage["arena_settings_open_v1"] (① is open the first time).

   Server API (§13.7): GET api/contract · PUT api/contract {markdown} · POST api/contract:reset
                       GET api/rules · PUT api/rules/{id} {markdown} · POST api/rules/{id}:reset · PUT|DELETE api/rules/{id}/materials/{slug}
                       POST api/bots/core:clear {ids?} · GET|PUT api/bots/{id}/core
                       GET api/games · GET api/games/{id}/detail · DELETE api/games/{id} · GET api/games/{id}/export · POST api/games/import

   ctx: { api, Core, Avatar, showToast, getSession(), openDialog(o), download(path) (fetch + save: the API wants the token as a header), buildUrl(path, {token}),
          archive {openSettings(), closeSettings(), goHome()} (archive.js), bots (bots.js instance), openHistory(id), onGameDeleted(res), applyBots(payload),
          storage?, document?, window? }
   ========================================================================== */

(function (root) {
  "use strict";

  const Lib = root.ArenaBotLib || (typeof require === "function" ? require("./botlib.js") : null);

  const OPEN_KEY = "arena_settings_open_v1";
  const COLUMNS = Object.freeze([
    { id: "archive", no: "①", title: "总存档" },
    { id: "core", no: "②", title: "底层设定" },
    { id: "contract", no: "③", title: "官方契约" },
    { id: "rules", no: "④", title: "规则" },
    { id: "games", no: "⑤", title: "对局管理" },
  ]);
  const COLUMN_IDS = COLUMNS.map((c) => c.id);
  /** Other names a caller may use for a column (`Bots.focus` / app.js: "bots" is the L0 column). */
  const COLUMN_ALIASES = Object.freeze({ bots: "core", l0: "core", export: "archive", import: "archive", settings: "archive", history: "games" });
  /** The id of the column a name stands for ("" when there is none). */
  function columnOf(name) {
    const n = String(name || "");
    return COLUMN_IDS.includes(n) ? n : COLUMN_ALIASES[n] || "";
  }
  /** The fixed sentences of §13.10 (the tests assert on them). */
  const STALE_SENTENCE = "所有 bot 将变为需重新初始化。";
  const CLEAR_CORE_SENTENCE = "清除后需要重新生成，否则无法开始对局；已有的初始化会变为需重新初始化。";
  const DELETE_GAME_SENTENCE = "删除这局的存档和日志。bot 已经通过复盘形成的记忆不会被删除，也无法通过删除对局撤销。";
  /** The second sentence of it: what the server answers as `note` and the toast after a delete repeats. */
  const DELETE_GAME_NOTE = "bot 已经通过复盘形成的记忆不会被删除，也无法通过删除对局撤销。";

  // ---------------------------------------------------------------------------------------------------------------------
  // pure helpers
  // ---------------------------------------------------------------------------------------------------------------------

  /** Which columns are open: {archive: true, ...} from the stored text; column ① open when there is nothing (usable) stored. */
  function parseOpen(text) {
    let raw = null;
    try {
      raw = text ? JSON.parse(text) : null;
    } catch {
      raw = null;
    }
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return { archive: true };
    const out = {};
    for (const id of COLUMN_IDS) if (typeof raw[id] === "boolean") out[id] = raw[id];
    return out;
  }

  /** 1536 -> "1.5 KB"; 3 MiB -> "3.0 MB" (the sizes of games and logs). */
  function formatSize(n) {
    const b = Number(n);
    if (!Number.isFinite(b) || b < 0) return "—";
    if (b < 1024) return `${Math.round(b)} B`;
    if (b < 1048576) return `${(b / 1024).toFixed(b < 10240 ? 1 : 0)} KB`;
    if (b < 1073741824) return `${(b / 1048576).toFixed(b < 10485760 ? 1 : 0)} MB`;
    return `${(b / 1073741824).toFixed(1)} GB`;
  }

  const shortHash = (h) => (h ? `${String(h).slice(0, 12)}…` : "—");

  /** A standard slug for a study material out of a file name: lower case, letters / digits / hyphens, at most 32, starts with a letter or a digit. */
  function slugOfFileName(name) {
    const base = String(name || "").replace(/^.*[\\/]/, "").replace(/\.[^.]*$/, "");
    let slug = base.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 32).replace(/-+$/, "");
    if (!/^[a-z0-9]/.test(slug)) slug = "";
    return slug;
  }

  const SLUG_RE = /^[a-z0-9][a-z0-9-]{0,31}$/;

  /** An ISO time ("2026-11-18T09:00:00Z") or unix seconds as "2026-11-18 01:00" in the viewer's time ("" when unusable). */
  function formatWhen(v) {
    if (v === undefined || v === null || v === "") return "";
    const d = typeof v === "number" ? new Date(v * 1000) : new Date(v);
    if (Number.isNaN(d.getTime())) return "";
    const p = (x) => String(x).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  /** "m:ss" / "h:mm:ss" of game seconds. */
  function formatGameTime(seconds) {
    const n = Number(seconds);
    if (!Number.isFinite(n) || n <= 0) return "";
    const s = Math.round(n);
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = String(s % 60).padStart(2, "0");
    return h > 0 ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
  }

  /** What a row of the games list says: {title, meta[], status, review, size, canDelete, deleteTitle, canExport, ...}.  `live`: {status} of the live game. */
  function gameRow(game, live, Core) {
    const unreadable = game.readable === false || game.status === "unreadable";
    const current = Boolean(game.current);
    const status = current && live && live.status ? live.status : game.status;
    let statusText;
    if (unreadable) statusText = "无法读取";
    else if (status === "finished") statusText = game.winner ? `已结束 · ${game.winner} 获胜` : "已结束";
    else if (status === "running") statusText = "进行中";
    else if (status === "paused") statusText = "已暂停";
    else statusText = "未开始";
    const review = game.review && typeof game.review === "object" ? game.review : {};
    const total = review.total === null || review.total === undefined ? null : Number(review.total);
    // (`total` is null for a game that cannot be reviewed - a game of the old format - and while the game is not over; the title says which)
    const reviewText = unreadable ? "" : total === null ? "复盘 —" : `复盘 ${Number(review.done) || 0}/${total}`;
    const reviewTitle = game.schema_version === 5 ? "旧版（v5）存档：没有 bot，不能复盘" : total === null ? "对局结束后才能复盘" : "已复盘的 bot / 全部 bot";
    const bytes = (Number(game.size_bytes) || 0) + (Number(game.log_bytes) || 0);
    const running = current && status === "running";
    return {
      id: game.session_id,
      title: game.scenario_title || game.title || game.session_id,
      current,
      human: Boolean(game.human_involved),
      unreadable,
      status,
      statusText,
      when: formatWhen(game.created_at || game.updated_at),
      duration: game.duration_gt > 0 ? `用时 ${formatGameTime(game.duration_gt)}` : "",
      cost: game.cost_usd > 0 && Core ? Core.formatCost(game.cost_usd, 2) : "",
      reviewText,
      reviewTitle,
      size: unreadable ? "" : `${formatSize(bytes)}${game.log_bytes ? `（日志 ${formatSize(game.log_bytes)}）` : ""}`,
      players: Array.isArray(game.participants_brief) ? game.participants_brief : [],
      canDelete: !running,
      deleteTitle: running ? "进行中的对局不能删除，请先暂停。" : current ? "这是当前对局：删除后会新建一局空白对局。" : "删除这局的存档和日志",
      canExport: !unreadable,
      exportTitle: unreadable ? "这局的存档无法读取，不能导出。" : "导出这一局（.arenagame）",
    };
  }

  // ---------------------------------------------------------------------------------------------------------------------
  // the UI
  // ---------------------------------------------------------------------------------------------------------------------

  const errText = (e) => (e && (e.detail || e.message)) || "操作失败";

  function create(ctx) {
    const opts = ctx || {};
    const Core = opts.Core || root.ArenaCore;
    const Avatar = opts.Avatar || root.ArenaAvatar;
    const api = opts.api;
    const doc = opts.document || root.document;
    const win = opts.window || root;
    const showToast = opts.showToast || (() => {});
    const getSession = opts.getSession || (() => null);
    const archive = opts.archive || null;
    const botsUI = opts.bots || null;
    const openDialog = opts.openDialog || null;
    const buildUrl = opts.buildUrl || ((path) => path);
    if (!Core || !api || !doc || !Lib) return null;

    const esc = Core.escapeHtml;
    const enc = encodeURIComponent;
    const $ = (id) => (typeof doc.getElementById === "function" ? doc.getElementById(id) : null);
    const el = { body: $("settings-body"), modal: $("settings-modal"), fileInput: $("file-settings-import") };

    const storage = (() => {
      try {
        return opts.storage || win.localStorage || null;
      } catch {
        return null;
      }
    })();
    const readOpen = () => {
      try {
        return parseOpen(storage ? storage.getItem(OPEN_KEY) : null);
      } catch {
        return { archive: true };
      }
    };

    const st = {
      open: readOpen(),
      contract: { data: null, loading: false, error: "", preview: false, notice: null },
      rules: { data: null, loading: false, error: "", notice: null },
      games: { data: null, loading: false, error: "", detail: {}, expanded: {}, notice: null },
      core: { expanded: {}, preview: {}, notice: {} },
      busy: {},
      pick: null,
      last: {}, // column -> the HTML it was last drawn with
    };

    const session = () => getSession() || {};
    const botList = () => (session().bots && Array.isArray(session().bots.bots) ? session().bots.bots : []);
    const isRunning = () => session().status === "running";
    const isIdle = () => session().status === "idle" && !session().replay;
    const isOpen = () => Boolean(el.modal && el.modal.style && el.modal.style.display === "flex");

    function on(node, type, handler) {
      if (node && typeof node.addEventListener === "function") node.addEventListener(type, handler);
    }
    function closestFrom(target, selector) {
      let node = target;
      if (node && typeof node.closest !== "function" && node.parentElement) node = node.parentElement;
      return node && typeof node.closest === "function" ? node.closest(selector) : null;
    }
    async function confirm(o) {
      if (typeof openDialog === "function") return openDialog(o);
      const ok = typeof win.confirm === "function" ? win.confirm(String(o.title || "确认")) : false;
      return { ok, values: {} };
    }

    // -----------------------------------------------------------------------------------------------------------------
    // files: download, pick, read
    // -----------------------------------------------------------------------------------------------------------------

    async function download(path) {
      if (typeof opts.download === "function") {
        // (the GETs of these files need the token as a HEADER - a link cannot send one - so the page fetches and saves the bytes)
        try {
          await opts.download(path);
        } catch (e) {
          showToast(`下载失败：${errText(e)}`);
          return false;
        }
        return true;
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
      return true;
    }

    function pickFile(kind, accept, meta) {
      st.pick = { kind, ...(meta || {}) };
      const input = el.fileInput;
      if (!input) return;
      input.accept = accept;
      input.value = "";
      if (typeof input.click === "function") input.click();
    }

    async function readText(file) {
      if (file && typeof file.text === "function") return file.text();
      return new Promise((resolve, reject) => {
        const FR = win && win.FileReader;
        if (typeof FR !== "function") {
          reject(new Error("这个浏览器不能读取文件。"));
          return;
        }
        const r = new FR();
        r.onload = () => resolve(String(r.result || ""));
        r.onerror = () => reject(new Error("读取文件失败。"));
        r.readAsText(file, "utf-8");
      });
    }

    // -----------------------------------------------------------------------------------------------------------------
    // the frame: five columns
    // -----------------------------------------------------------------------------------------------------------------

    /** archive.js's home view -> the five columns; ① holds archive.js's own HTML, the others are filled by `onHomeRendered`. */
    function wrapHome(archiveHtml) {
      return COLUMNS.map((c) => {
        const open = Boolean(st.open[c.id]);
        return (
          `<section class="settings-col${open ? " open" : ""}" id="settings-col-${c.id}" data-col="${c.id}">` +
          `<button type="button" class="settings-col-head" id="settings-col-head-${c.id}" data-settings-action="toggle-col" data-col="${c.id}" aria-expanded="${open}">` +
          `<span class="caret" id="settings-col-caret-${c.id}" aria-hidden="true">${open ? "▼" : "▶"}</span>` +
          `<span class="settings-col-no">${c.no}</span><span class="settings-col-title">${esc(c.title)}</span>` +
          `<span class="settings-col-meta form-hint" id="settings-col-meta-${c.id}"></span></button>` +
          `<div class="settings-col-body" id="settings-col-body-${c.id}"${open ? "" : " hidden"}>${c.id === "archive" ? archiveHtml : ""}</div></section>`
        );
      }).join("");
    }

    function persistOpen() {
      try {
        if (storage) storage.setItem(OPEN_KEY, JSON.stringify(st.open));
      } catch {
        /* storage unavailable: the columns still work */
      }
    }

    function paintColumnState(id) {
      const open = Boolean(st.open[id]);
      const section = $(`settings-col-${id}`);
      if (section && section.classList) section.classList.toggle("open", open);
      const head = $(`settings-col-head-${id}`);
      if (head && typeof head.setAttribute === "function") head.setAttribute("aria-expanded", open ? "true" : "false");
      const caret = $(`settings-col-caret-${id}`);
      if (caret) caret.textContent = open ? "▼" : "▶";
      const body = $(`settings-col-body-${id}`);
      if (body) body.hidden = !open;
    }

    function setOpen(id, open, { persist = true } = {}) {
      if (!COLUMN_IDS.includes(id)) return;
      st.open[id] = Boolean(open);
      if (persist) persistOpen();
      paintColumnState(id);
      if (open) {
        renderColumn(id);
        ensureLoaded(id);
      }
    }

    function renderMeta() {
      const meta = {};
      const list = botList();
      if (list.length) meta.core = `${list.filter((b) => b.core && b.core.complete).length}/${list.length} 完整`;
      const c = st.contract.data;
      if (c) {
        const origin = c.source && c.source.origin === "imported" ? "已导入" : "默认";
        meta.contract = `${origin}${c.source && c.source.version ? ` · ${c.source.version}` : ""}${(c.problems || []).length ? " · ⚠ 有问题" : ""}`;
      }
      const r = st.rules.data;
      if (r) meta.rules = `${r.length} 个剧本 · ${r.reduce((n, s) => n + (s.materials || []).length, 0)} 份材料${r.some((s) => s.problem) ? " · ⚠ 有问题" : ""}`;
      const g = st.games.data;
      if (g) meta.games = `${g.length} 局`;
      for (const id of COLUMN_IDS) {
        const node = $(`settings-col-meta-${id}`);
        if (node && meta[id] !== undefined) node.textContent = meta[id];
      }
    }

    function renderColumn(id) {
      if (id === "archive") return;
      const node = $(`settings-col-body-${id}`);
      if (!node) return;
      const html = { core: coreHtml, contract: contractHtml, rules: rulesHtml, games: gamesHtml }[id]();
      if (st.last[id] === html && node.innerHTML === html) return;
      st.last[id] = html;
      node.innerHTML = html;
    }

    /** archive.js drew its home view (the five frames): fill the open columns, load what they need. */
    function onHomeRendered() {
      st.last = {};
      for (const id of COLUMN_IDS) {
        paintColumnState(id);
        if (st.open[id]) {
          renderColumn(id);
          ensureLoaded(id);
        }
      }
      renderMeta();
    }

    function ensureLoaded(id) {
      if (id === "contract" && !st.contract.data && !st.contract.loading) loadContract();
      else if (id === "rules" && !st.rules.data && !st.rules.loading) loadRules();
      else if (id === "games" && !st.games.data && !st.games.loading) loadGames();
    }

    /** The dialog was opened (archive.js): everything the columns show is read again - also the closed ones, for the line next to their title. */
    function onOpened() {
      if (!st.contract.loading) loadContract();
      if (!st.rules.loading) loadRules();
      if (!st.games.loading) loadGames();
    }

    /** Open the dialog (at `column` when one is named).  Resolves whether the dialog could be opened. */
    function open(name) {
      if (!archive) return false;
      const column = columnOf(name);
      if (column) {
        st.open[column] = true;
        persistOpen();
      }
      archive.openSettings();
      if (column && typeof archive.goHome === "function") archive.goHome(); // (the columns are the home view: a result / progress view would hide them)
      if (column) {
        paintColumnState(column);
        const node = $(`settings-col-${column}`);
        if (node && typeof node.scrollIntoView === "function") node.scrollIntoView({ block: "start" });
        if (column !== "archive") {
          renderColumn(column);
          ensureLoaded(column);
        }
      }
      return true;
    }

    // -----------------------------------------------------------------------------------------------------------------
    // ② 底层设定
    // -----------------------------------------------------------------------------------------------------------------

    function coreRowHtml(b) {
      const open = Boolean(st.core.expanded[b.id]);
      const v = Lib.initView(b);
      const chars = Lib.CORE_KEYS.map((k) => `${Lib.CORE_HEADINGS[k]} ${(b.core.chars && b.core.chars[k]) || 0}`).join(" · ") + (b.core.extras.length ? ` · 附加 ${b.core.extras.length} 份` : "");
      const state = b.core.complete ? `<span class="srow-state ok">完整</span>` : `<span class="srow-state bad">缺少：${esc((b.core.missing || []).map((k) => Lib.CORE_HEADINGS[k]).join("、"))}</span>`;
      const notice = st.core.notice[b.id];
      const preview = st.core.preview[b.id];
      const busy = Boolean(st.busy[`core:${b.id}`]);
      const initBusy = b.init.status === "initializing";
      let body = "";
      if (open) {
        const text = preview ? (preview.loading ? "正在读取…" : preview.error ? preview.error : preview.text || "（这个玩家还没有底层设定）") : "正在读取…";
        body =
          `<div class="settings-row-body">` +
          `<pre class="settings-pre" id="settings-core-preview-${esc(b.id)}">${esc(text)}</pre>` +
          (notice && notice.text ? `<div class="${notice.kind === "error" ? "form-error" : "form-hint"}">${esc(notice.text)}</div>` : "") +
          (notice && notice.problems && notice.problems.length ? `<ul class="form-error editor-problems">${notice.problems.map((p) => `<li>${esc(p)}</li>`).join("")}</ul>` : "") +
          `<div class="settings-actions">` +
          `<button type="button" class="btn btn-xs" data-settings-action="core-export" data-bot="${esc(b.id)}"${b.core.updated_at || b.core.complete ? "" : " disabled"}>导出 .md</button>` +
          `<button type="button" class="btn btn-xs" data-settings-action="core-import" data-bot="${esc(b.id)}"${busy || initBusy ? " disabled" : ""}>导入 .md…</button>` +
          `<button type="button" class="btn btn-xs btn-primary-outline" data-settings-action="core-gen" data-bot="${esc(b.id)}"` +
          ` title="打开这个玩家的面板：写几个要点，让主持人模型整理成底层设定（草稿先给你看）">✨ 生成 / 完善…</button>` +
          `<button type="button" class="btn btn-xs" data-settings-action="core-default" data-bot="${esc(b.id)}"${isIdle() ? "" : " disabled"}` +
          ` title="${isIdle() ? "打开这个玩家的面板：从默认库换成 Alice … Leo 之一（会连名字、头像、记忆一起换）" : "对局开始之后不能从默认库导入"}">从默认库套用…</button>` +
          `</div></div>`;
      }
      const avatar = Avatar && b.avatar ? Avatar.imgHtml(b.seat || b.id, undefined, b.avatar, { size: 28 }) : "";
      // the head of a row: the toggle takes the line, 清除 is a quiet text button at its right end (red only under the pointer): twelve of them must not be twelve red boxes
      return (
        `<li class="settings-row${open ? " open" : ""}" data-bot="${esc(b.id)}">` +
        `<div class="settings-row-head">` +
        `<button type="button" class="settings-row-main" data-settings-action="core-toggle" data-bot="${esc(b.id)}" aria-expanded="${open}">` +
        `<span class="srow-avatar">${avatar}</span>` +
        `<span class="srow-name">${esc(b.name)}</span><span class="form-hint srow-seat">${esc(b.seat || "")}</span>` +
        `<span class="srow-init init-${esc(v.status)}" title="${esc(v.label)}">${v.symbol}</span>${state}` +
        `<span class="form-hint srow-chars">${esc(chars)}</span>` +
        `<span class="form-hint srow-time">${esc(formatTimeOf(b.core.updated_at))}</span>` +
        `<span class="caret" aria-hidden="true">${open ? "▼" : "▶"}</span></button>` +
        `<span class="srow-actions"><button type="button" class="btn-quiet danger" data-settings-action="core-clear" data-bot="${esc(b.id)}"${busy || initBusy ? " disabled" : ""}` +
        ` title="清空这个玩家的底层设定（名字、头像、记忆不变）">清除</button></span></div>${body}</li>`
      );
    }

    const formatTimeOf = (ts) => Lib.formatUnix(ts);

    function coreHtml() {
      const list = botList();
      if (!list.length) return `<div class="form-hint">还没有读到玩家（bot）。</div>`;
      const busy = Boolean(st.busy["core:all"]);
      const anyCore = list.some((b) => b.core && (b.core.updated_at || b.core.complete || (b.core.missing || []).length < 3));
      return (
        `<div class="settings-actions"><button type="button" class="btn btn-xs btn-danger-soft" id="btn-settings-core-clear-all" data-settings-action="core-clear-all"${busy || !anyCore ? " disabled" : ""}>全部清除…</button>` +
        `<span class="form-hint">${list.length} 个玩家的底层设定（身份 / 说话方式 / 分析方式 / 附加）。名字、头像和记忆不受影响。</span></div>` +
        `<ul class="settings-rows" id="settings-core-rows">${list.map(coreRowHtml).join("")}</ul>`
      );
    }

    async function loadCorePreview(botId) {
      st.core.preview[botId] = { loading: true };
      renderColumn("core");
      try {
        const res = await api.get(`api/bots/${enc(botId)}/core`);
        st.core.preview[botId] = { text: res && typeof res.markdown === "string" ? res.markdown : "" };
      } catch (e) {
        st.core.preview[botId] = { error: `读取失败：${errText(e)}` };
      }
      renderColumn("core");
    }

    function toggleCoreRow(botId) {
      st.core.expanded[botId] = !st.core.expanded[botId];
      if (st.core.expanded[botId]) loadCorePreview(botId);
      else renderColumn("core");
    }

    async function clearCore(ids) {
      const list = botList();
      const named = ids ? list.filter((b) => ids.includes(b.id)) : list;
      const one = ids && ids.length === 1 ? named[0] : null;
      const choice = await confirm({
        title: one ? `清除 ${one.name} 的底层设定` : "清除全部底层设定",
        bodyHtml: `<div>${one ? `将清空 <strong>${esc(one.name)}</strong> 的底层设定。` : `将清空<strong>全部 ${list.length} 个玩家</strong>的底层设定。`}${esc(CLEAR_CORE_SENTENCE)}</div>`,
        okText: "确认清除",
        danger: true,
      });
      if (!choice || !choice.ok) return false;
      const key = one ? `core:${one.id}` : "core:all";
      st.busy[key] = true;
      renderColumn("core");
      try {
        const res = await api.post("api/bots/core:clear", ids ? { ids } : {});
        if (res && typeof res === "object" && opts.applyBots) opts.applyBots(res);
        else if (botsUI) await botsUI.refreshBots();
        for (const b of named) {
          delete st.core.preview[b.id];
          if (st.core.expanded[b.id]) loadCorePreview(b.id);
        }
        showToast(one ? `${one.name} 的底层设定已清除` : "全部底层设定已清除");
        return true;
      } catch (e) {
        showToast(`清除失败：${errText(e)}`);
        return false;
      } finally {
        delete st.busy[key];
        renderColumn("core");
        renderMeta();
      }
    }

    async function importCore(botId, file) {
      const editors = botsUI && botsUI.editors;
      if (!editors) return false;
      st.core.notice[botId] = null;
      const ok = await editors.importCoreFile(botId, file);
      const err = editors.coreIssue ? editors.coreIssue(botId) : null;
      if (ok) {
        delete st.core.preview[botId];
        st.core.notice[botId] = { kind: "ok", text: `已导入：${file.name}` };
        if (st.core.expanded[botId]) loadCorePreview(botId);
      } else if (err && (err.error || (err.problems && err.problems.length))) {
        st.core.notice[botId] = { kind: "error", text: err.error || "", problems: err.problems || [] };
      }
      renderColumn("core");
      return ok;
    }

    // -----------------------------------------------------------------------------------------------------------------
    // ③ 官方契约
    // -----------------------------------------------------------------------------------------------------------------

    async function loadContract() {
      const first = !st.contract.data;
      st.contract.loading = true;
      st.contract.error = "";
      if (first) renderColumn("contract"); // (a refresh keeps showing what it has until the answer comes)
      try {
        st.contract.data = await api.get("api/contract");
      } catch (e) {
        st.contract.error = `读取官方契约失败：${errText(e)}`;
      }
      st.contract.loading = false;
      renderColumn("contract");
      renderMeta();
    }

    function contractHtml() {
      const c = st.contract;
      if (c.loading && !c.data) return `<div class="form-hint">正在读取…</div>`;
      if (c.error) return `<div class="form-error">${esc(c.error)}</div><div class="settings-actions"><button type="button" class="btn btn-xs" data-settings-action="contract-refresh">重试</button></div>`;
      const d = c.data;
      if (!d) return `<div class="form-hint">还没有读取。</div>`;
      const src = d.source || {};
      const imported = src.origin === "imported";
      const problems = [...(d.problems || []), ...((c.notice && c.notice.problems) || [])].filter((p, i, a) => a.indexOf(p) === i);
      const busy = Boolean(st.busy.contract);
      const must = Number(d.must_read_chars) || 0;
      return (
        `<dl class="settings-facts archive-facts" id="settings-contract-facts">` +
        `<dt>来源</dt><dd id="settings-contract-origin">${imported ? `导入的文件${src.imported_at ? `（${esc(formatWhen(src.imported_at))}）` : ""}` : "默认（随程序发布）"}</dd>` +
        `<dt>版本</dt><dd>${esc(src.version || "—")}</dd>` +
        `<dt>字数</dt><dd>${esc(String(d.chars || 0))} 字 · 必读 ${esc(String(must))} / 1200</dd>` +
        `<dt>SHA-256</dt><dd><code title="${esc(src.sha256 || "")}">${esc(shortHash(src.sha256))}</code></dd></dl>` +
        (problems.length
          ? `<div class="form-error" id="settings-contract-problems"><div>${(d.problems || []).length ? "这份契约现在不能用：" : "这份文件没有被采用："}</div><ul class="editor-problems">${problems.map((p) => `<li>${esc(p)}</li>`).join("")}</ul></div>`
          : `<div class="form-hint">契约检查通过：所有工具名和回执码都在里面，必读部分没有超长。</div>`) +
        (c.notice && c.notice.text ? `<div class="form-hint">${esc(c.notice.text)}</div>` : "") +
        `<div class="settings-actions">` +
        `<button type="button" class="btn btn-xs" data-settings-action="contract-toggle" aria-expanded="${c.preview}">${c.preview ? "收起预览" : "预览"}</button>` +
        `<button type="button" class="btn btn-xs" id="btn-settings-contract-export" data-settings-action="contract-export">导出</button>` +
        `<button type="button" class="btn btn-xs btn-primary-outline" id="btn-settings-contract-import" data-settings-action="contract-import"${busy ? " disabled" : ""}>导入…</button>` +
        `<button type="button" class="btn btn-xs" id="btn-settings-contract-reset" data-settings-action="contract-reset"${busy || !imported ? " disabled" : ""}` +
        ` title="${imported ? "丢掉导入的契约，用默认契约" : "现在用的就是默认契约"}">恢复默认</button></div>` +
        (c.preview ? `<pre class="settings-pre" id="settings-contract-preview">${esc(d.markdown || "")}</pre>` : "")
      );
    }

    async function importContract(file) {
      let text;
      try {
        text = await readText(file);
      } catch (e) {
        st.contract.notice = { problems: [errText(e)] };
        renderColumn("contract");
        return false;
      }
      const choice = await confirm({
        title: "导入官方契约",
        bodyHtml: `<div>将用文件 <strong>${esc(file.name)}</strong>（${Lib.textLength(text)} 字）替换官方契约。</div><div>${esc(STALE_SENTENCE)}</div>`,
        okText: "导入",
      });
      if (!choice || !choice.ok) return false;
      st.busy.contract = true;
      st.contract.notice = null;
      renderColumn("contract");
      try {
        st.contract.data = await api.put("api/contract", { markdown: text });
        st.contract.notice = { text: `已导入：${file.name}。${STALE_SENTENCE}` };
        showToast("官方契约已导入");
        if (botsUI) botsUI.refreshBots();
        return true;
      } catch (e) {
        const problems = Array.isArray(e && e.problems) ? e.problems : [];
        st.contract.notice = { problems: problems.length ? problems : [errText(e)] };
        showToast(errText(e));
        return false;
      } finally {
        delete st.busy.contract;
        renderColumn("contract");
        renderMeta();
      }
    }

    async function resetContract() {
      const choice = await confirm({
        title: "恢复默认契约",
        bodyHtml: `<div>将丢掉导入的契约，改用随程序发布的默认契约。</div><div>${esc(STALE_SENTENCE)}</div>`,
        okText: "恢复默认",
        danger: true,
      });
      if (!choice || !choice.ok) return false;
      st.busy.contract = true;
      renderColumn("contract");
      try {
        st.contract.data = await api.post("api/contract:reset", {});
        st.contract.notice = { text: `已恢复默认契约。${STALE_SENTENCE}` };
        showToast("已恢复默认契约");
        if (botsUI) botsUI.refreshBots();
        return true;
      } catch (e) {
        st.contract.notice = { problems: [errText(e)] };
        showToast(errText(e));
        return false;
      } finally {
        delete st.busy.contract;
        renderColumn("contract");
        renderMeta();
      }
    }

    // -----------------------------------------------------------------------------------------------------------------
    // ④ 规则
    // -----------------------------------------------------------------------------------------------------------------

    async function loadRules() {
      const first = !st.rules.data;
      st.rules.loading = true;
      st.rules.error = "";
      if (first) renderColumn("rules");
      try {
        const res = await api.get("api/rules");
        st.rules.data = res && Array.isArray(res.scenarios) ? res.scenarios : [];
      } catch (e) {
        st.rules.error = `读取规则失败：${errText(e)}`;
      }
      st.rules.loading = false;
      renderColumn("rules");
      renderMeta();
    }

    function scenarioHtml(s) {
      const imported = s.origin === "imported";
      const busy = Boolean(st.busy[`rules:${s.id}`]);
      const notice = st.rules.notice && st.rules.notice[s.id];
      const mats = s.materials || [];
      return (
        `<li class="settings-block" data-scenario="${esc(s.id)}">` +
        `<div class="settings-block-head"><strong class="srow-name">${esc(s.title || s.id)}</strong>` +
        `<span class="form-hint">${esc(s.id)}${s.version ? ` · v${esc(s.version)}` : ""}</span>` +
        `<span class="srow-state ${imported ? "warn" : "ok"}">${imported ? `导入${s.imported_at ? `（${esc(formatWhen(s.imported_at))}）` : ""}` : "默认"}</span>` +
        `<span class="form-hint">${esc(String(s.chars || 0))} 字 · ${esc(shortHash(s.sha256))}</span></div>` +
        (s.problem ? `<div class="form-error settings-problem">${esc(s.problem)}</div>` : "") +
        (notice && notice.problems && notice.problems.length
          ? `<ul class="form-error editor-problems">${notice.problems.map((p) => `<li>${esc(p)}</li>`).join("")}</ul>`
          : notice && notice.text
            ? `<div class="form-hint">${esc(notice.text)}</div>`
            : "") +
        `<div class="settings-actions">` +
        `<button type="button" class="btn btn-xs" data-settings-action="rules-export" data-id="${esc(s.id)}">导出</button>` +
        `<button type="button" class="btn btn-xs btn-primary-outline" data-settings-action="rules-import" data-id="${esc(s.id)}"${busy ? " disabled" : ""}>导入…</button>` +
        `<button type="button" class="btn btn-xs" data-settings-action="rules-reset" data-id="${esc(s.id)}"${busy || !imported ? " disabled" : ""}>恢复默认</button></div>` +
        `<div class="settings-sub"><div class="settings-sub-title">学习材料（${mats.length} 份）<span class="form-hint">bot 初始化时会读它们；增删后所有 bot 都要重新初始化。</span></div>` +
        (mats.length
          ? `<ul class="settings-rows">${mats
              .map(
                (m) =>
                  `<li class="settings-row mat-row" data-slug="${esc(m.slug)}"><span class="srow-name">${esc(m.title || m.slug)}</span>` +
                  `<span class="form-hint">${esc(m.slug)} · ${esc(String(m.chars || 0))} 字 · ${esc(shortHash(m.sha256))}</span>` +
                  `<span class="srow-actions"><button type="button" class="btn-quiet" data-settings-action="mat-export" data-id="${esc(s.id)}" data-slug="${esc(m.slug)}">导出</button>` +
                  `<button type="button" class="btn-quiet danger" data-settings-action="mat-delete" data-id="${esc(s.id)}" data-slug="${esc(m.slug)}"${busy ? " disabled" : ""}>删除</button></span></li>`
              )
              .join("")}</ul>`
          : `<div class="form-hint">这个剧本没有学习材料。</div>`) +
        `<div class="settings-actions"><button type="button" class="btn btn-xs" data-settings-action="mat-add" data-id="${esc(s.id)}"${busy || mats.length >= 20 ? " disabled" : ""}>＋ 添加学习材料…</button></div></div></li>`
      );
    }

    function rulesHtml() {
      const r = st.rules;
      if (r.loading && !r.data) return `<div class="form-hint">正在读取…</div>`;
      if (r.error) return `<div class="form-error">${esc(r.error)}</div><div class="settings-actions"><button type="button" class="btn btn-xs" data-settings-action="rules-refresh">重试</button></div>`;
      if (!r.data) return `<div class="form-hint">还没有读取。</div>`;
      if (!r.data.length) return `<div class="form-hint">没有剧本。</div>`;
      return (
        `<div class="form-hint" id="settings-rules-note">剧本文件同时决定主持人手册和游戏机制，所以导入的是整份剧本；改动之后所有 bot 都要重新初始化。</div>` +
        `<ul class="settings-blocks" id="settings-rules-list">${r.data.map(scenarioHtml).join("")}</ul>`
      );
    }

    /** Replace one scenario of the list by the answer of the server (a RulesItem). */
    function putScenario(item) {
      if (!item || typeof item !== "object" || !item.id) return;
      const list = st.rules.data || [];
      const at = list.findIndex((s) => s.id === item.id);
      if (at >= 0) list[at] = item;
      else list.push(item);
      st.rules.data = list;
    }

    function ruleNotice(id, notice) {
      st.rules.notice = { ...(st.rules.notice || {}), [id]: notice };
    }

    async function writeRules(id, label, call, doneText) {
      st.busy[`rules:${id}`] = true;
      ruleNotice(id, null);
      renderColumn("rules");
      try {
        putScenario(await call());
        ruleNotice(id, { text: `${doneText}${STALE_SENTENCE}` });
        showToast(doneText);
        if (botsUI) botsUI.refreshBots();
        return true;
      } catch (e) {
        const problems = Array.isArray(e && e.problems) ? e.problems : [];
        ruleNotice(id, { problems: problems.length ? problems : [errText(e)] });
        showToast(`${label}失败：${errText(e)}`);
        return false;
      } finally {
        delete st.busy[`rules:${id}`];
        renderColumn("rules");
        renderMeta();
      }
    }

    async function importRules(id, file) {
      let text;
      try {
        text = await readText(file);
      } catch (e) {
        ruleNotice(id, { problems: [errText(e)] });
        renderColumn("rules");
        return false;
      }
      const choice = await confirm({
        title: "导入剧本",
        bodyHtml: `<div>将用文件 <strong>${esc(file.name)}</strong>（${Lib.textLength(text)} 字）替换剧本 <strong>${esc(id)}</strong>。</div><div>${esc(STALE_SENTENCE)}</div>`,
        okText: "导入",
      });
      if (!choice || !choice.ok) return false;
      return writeRules(id, "导入剧本", () => api.put(`api/rules/${enc(id)}`, { markdown: text }), `剧本 ${id} 已导入。`);
    }

    async function resetRules(id) {
      const choice = await confirm({
        title: "恢复默认剧本",
        bodyHtml: `<div>将丢掉导入的版本，改用随程序发布的默认剧本 <strong>${esc(id)}</strong>。</div><div>${esc(STALE_SENTENCE)}</div>`,
        okText: "恢复默认",
        danger: true,
      });
      if (!choice || !choice.ok) return false;
      return writeRules(id, "恢复默认", () => api.post(`api/rules/${enc(id)}:reset`, {}), `剧本 ${id} 已恢复默认。`);
    }

    async function addMaterial(id, file) {
      let text;
      try {
        text = await readText(file);
      } catch (e) {
        ruleNotice(id, { problems: [errText(e)] });
        renderColumn("rules");
        return false;
      }
      const slug0 = slugOfFileName(file.name);
      const choice = await confirm({
        title: "添加学习材料",
        bodyHtml:
          `<div>将把文件 <strong>${esc(file.name)}</strong>（${Lib.textLength(text)} 字）作为剧本 <strong>${esc(id)}</strong> 的学习材料。</div>` +
          `<div class="form-group"><label class="form-label" for="input-material-slug">标识（小写字母、数字和连字符，最多 32 个；同名的会被替换）</label>` +
          `<input type="text" class="form-input" id="input-material-slug" value="${esc(slug0)}" autocomplete="off" spellcheck="false" /></div>` +
          `<div class="form-group"><label class="form-label" for="input-material-title">标题（可以不填：取文中的第一个标题）</label>` +
          `<input type="text" class="form-input" id="input-material-title" value="" autocomplete="off" /></div>` +
          `<div>${esc(STALE_SENTENCE)}</div>`,
        okText: "添加",
        collect: () => {
          const slugBox = $("input-material-slug");
          const titleBox = $("input-material-title");
          return { slug: slugBox ? String(slugBox.value || "").trim() : slug0, title: titleBox ? String(titleBox.value || "").trim() : "" };
        },
      });
      if (!choice || !choice.ok) return false;
      const slug = (choice.values && choice.values.slug) || slug0;
      if (!SLUG_RE.test(slug)) {
        ruleNotice(id, { problems: ["材料的标识只能用小写字母、数字和连字符（最多 32 个字符，以字母或数字开头）。"] });
        renderColumn("rules");
        return false;
      }
      const body = { markdown: text };
      if (choice.values && choice.values.title) body.title = choice.values.title;
      return writeRules(id, "添加材料", () => api.put(`api/rules/${enc(id)}/materials/${enc(slug)}`, body), `学习材料 ${slug} 已添加。`);
    }

    async function deleteMaterial(id, slug) {
      const choice = await confirm({
        title: "删除学习材料",
        bodyHtml: `<div>将删除剧本 <strong>${esc(id)}</strong> 的学习材料 <strong>${esc(slug)}</strong>。</div><div>${esc(STALE_SENTENCE)}</div>`,
        okText: "删除",
        danger: true,
      });
      if (!choice || !choice.ok) return false;
      return writeRules(id, "删除材料", () => api.del(`api/rules/${enc(id)}/materials/${enc(slug)}`), `学习材料 ${slug} 已删除。`);
    }

    // -----------------------------------------------------------------------------------------------------------------
    // ⑤ 对局管理
    // -----------------------------------------------------------------------------------------------------------------

    async function loadGames() {
      const first = !st.games.data;
      st.games.loading = true;
      st.games.error = "";
      if (first) renderColumn("games");
      try {
        const res = await api.get("api/games");
        const list = Array.isArray(res) ? res : res && Array.isArray(res.games) ? res.games : [];
        st.games.data = list.filter((g) => g && typeof g === "object" && g.session_id).map((g) => ({ ...g, session_id: String(g.session_id) }));
      } catch (e) {
        st.games.error = `读取对局失败：${errText(e)}`;
      }
      st.games.loading = false;
      renderColumn("games");
      renderMeta();
    }

    async function loadGameDetail(id) {
      st.games.detail[id] = { loading: true };
      renderColumn("games");
      try {
        st.games.detail[id] = { data: await api.get(`api/games/${enc(id)}/detail`) };
      } catch (e) {
        st.games.detail[id] = { error: `读取详情失败：${errText(e)}` };
      }
      renderColumn("games");
    }

    function detailHtml(id) {
      const d = st.games.detail[id];
      if (!d || d.loading) return `<div class="settings-row-body"><div class="form-hint">正在读取…</div></div>`;
      if (d.error) return `<div class="settings-row-body"><div class="form-error">${esc(d.error)}</div></div>`;
      const data = d.data || {};
      // (the server says `pending` for any seat that has a bot, also while the game is not over: then there is nothing to review yet)
      const game = gameOf(id);
      const over = (id === String(session().session_id || "") ? session().status : game && game.status) === "finished";
      const REVIEW = { done: "已复盘", pending: over ? "未复盘" : "—", na: "—" };
      const rows = (data.participants || [])
        .map(
          (p) =>
            `<tr><td>${esc(String(p.seat || "").replace(/^N/, ""))}</td><td>${esc(p.name || "")}</td>` +
            `<td>${p.role ? esc(p.role) : "—"}${p.faction ? ` <span class="form-hint">${esc(p.faction)}</span>` : ""}</td><td>${esc(REVIEW[p.review] || "—")}</td></tr>`
        )
        .join("");
      const f = data.files || {};
      const spans = (data.human_spans || [])
        .map((h) => `${String(h.seat || "").replace(/^N/, "")} 号由人类代打（第 ${h.from_seq} ${h.to_seq === null || h.to_seq === undefined ? "条起，一直到结束" : `– ${h.to_seq} 条`}）`)
        .join("；");
      return (
        `<div class="settings-row-body" id="settings-game-detail-${esc(id)}">` +
        `<div class="settings-table-wrap"><table class="settings-table"><thead><tr><th>座位</th><th>名字</th><th>身份</th><th>复盘</th></tr></thead><tbody>${rows}</tbody></table></div>` +
        `<div class="form-hint">文件：对局存档 ${esc(formatSize(f.game_bytes))}${f.prev_bytes ? `，上一个存档点 ${esc(formatSize(f.prev_bytes))}` : ""}，日志 ${esc(formatSize(f.log_bytes))}</div>` +
        (spans ? `<div class="form-hint">${esc(spans)}</div>` : "") +
        `</div>`
      );
    }

    function gameRowHtml(game) {
      const live = { status: session().status || "idle" };
      const row = gameRow(game, live, Core);
      const open = Boolean(st.games.expanded[row.id]);
      const busy = Boolean(st.busy[`game:${row.id}`]);
      // the same head as a row of ②: the toggle takes the line, the actions are quiet text buttons at its right end (they go under it where the line is too narrow)
      return (
        `<li class="settings-row game-row${open ? " open" : ""}${row.current ? " is-current" : ""}${row.unreadable ? " is-unreadable" : ""}" data-game="${esc(row.id)}">` +
        `<div class="settings-row-head">` +
        `<button type="button" class="settings-row-main" data-settings-action="games-toggle" data-id="${esc(row.id)}" aria-expanded="${open}"${row.unreadable ? " disabled" : ""}>` +
        `<span class="srow-name">${esc(row.title)}</span>${row.current ? ` <span class="tag-badge muted">当前</span>` : ""}${row.human ? ` <span class="tag-badge purple" title="这局里有人接管过一个座位">🎮</span>` : ""}` +
        `<span class="form-hint srow-time">${esc(row.when)}</span><span class="srow-state ${row.status === "finished" ? "ok" : ""}">${esc(row.statusText)}</span>` +
        `<span class="form-hint srow-chars" title="${esc(row.reviewTitle)}">${esc([row.duration, row.cost, row.reviewText, row.size].filter(Boolean).join(" · "))}</span>` +
        `<span class="caret" aria-hidden="true">${open ? "▼" : "▶"}</span></button>` +
        `<span class="srow-actions">` +
        `<button type="button" class="btn-quiet" data-settings-action="games-export" data-id="${esc(row.id)}"${row.canExport && !busy ? "" : " disabled"} title="${esc(row.exportTitle)}">导出</button>` +
        `<button type="button" class="btn-quiet danger" data-settings-action="games-delete" data-id="${esc(row.id)}"${row.canDelete && !busy ? "" : " disabled"} title="${esc(row.deleteTitle)}">删除</button>` +
        `<button type="button" class="btn-quiet" data-settings-action="games-history" data-id="${esc(row.id)}" title="在历史对局里重放或继续这一局">历史对局…</button></span></div>` +
        (open ? detailHtml(row.id) : "") +
        `</li>`
      );
    }

    function gamesHtml() {
      const g = st.games;
      const top =
        `<div class="settings-actions">` +
        `<button type="button" class="btn btn-xs btn-primary-outline" id="btn-settings-games-import" data-settings-action="games-import"${st.busy["games:import"] ? " disabled" : ""}>${st.busy["games:import"] ? "正在导入…" : "导入对局…"}</button>` +
        `<button type="button" class="btn btn-xs" data-settings-action="games-refresh">刷新</button>` +
        `<button type="button" class="btn btn-xs" id="btn-settings-games-history" data-settings-action="games-history">历史对局（重放 / 继续）…</button></div>`;
      if (g.loading && !g.data) return `${top}<div class="form-hint">正在读取…</div>`;
      if (g.error) return `${top}<div class="form-error">${esc(g.error)}</div>`;
      if (!g.data) return `${top}<div class="form-hint">还没有读取。</div>`;
      return (
        top +
        (g.notice ? `<div class="${g.notice.kind === "error" ? "form-error" : "form-hint"}" id="settings-games-notice">${esc(g.notice.text)}</div>` : "") +
        (g.data.length ? `<ul class="settings-rows" id="settings-games-rows">${g.data.map(gameRowHtml).join("")}</ul>` : `<div class="form-hint">还没有保存过的对局。</div>`)
      );
    }

    const gameOf = (id) => (st.games.data || []).find((g) => g.session_id === id) || null;

    /** .arenagame of a game: fetched and saved by the page; the refusals (the current game while it runs, a save that cannot be opened) are said in the column. */
    async function exportGame(id) {
      st.games.notice = null;
      const key = `game:${id}`;
      st.busy[key] = true;
      renderColumn("games");
      try {
        if (typeof opts.download === "function") await opts.download(`api/games/${enc(id)}/export`);
        else await download(`api/games/${enc(id)}/export`);
        return true;
      } catch (e) {
        st.games.notice = { kind: "error", text: errText(e) };
        showToast(errText(e));
        return false;
      } finally {
        delete st.busy[key];
        renderColumn("games");
      }
    }
    const labelOf = (g) => {
      const when = formatWhen(g.created_at || g.updated_at);
      return `${g.scenario_title || g.title || g.session_id}${when ? `（${when}）` : ""}`;
    };

    async function deleteGame(id) {
      const game = gameOf(id);
      if (!game) return false;
      const row = gameRow(game, { status: session().status || "idle" }, Core);
      if (!row.canDelete) {
        showToast(row.deleteTitle);
        return false;
      }
      const choice = await confirm({
        title: "删除对局",
        bodyHtml:
          `<div>${esc(DELETE_GAME_SENTENCE)}</div><div class="form-hint">要删除的是：<strong>${esc(labelOf(game))}</strong></div>` +
          (row.current ? `<div class="form-hint">这是当前对局：删除后会新建一局空白对局。</div>` : ""),
        okText: "删除",
        danger: true,
      });
      if (!choice || !choice.ok) return false;
      st.busy[`game:${id}`] = true;
      renderColumn("games");
      try {
        const res = await api.del(`api/games/${enc(id)}`);
        showToast(`对局已删除（${Core.stripEndPunct((res && res.note) || DELETE_GAME_NOTE)}）`); // (the note is a sentence with its own full stop)
        delete st.games.detail[id];
        delete st.games.expanded[id];
        if (opts.onGameDeleted) opts.onGameDeleted(res || {});
        await loadGames();
        return true;
      } catch (e) {
        st.games.notice = { kind: "error", text: errText(e) };
        showToast(errText(e));
        // a file that is in use (409 in_use) for the CURRENT game: the server made the empty new game all the same - the page follows it
        if (e && e.body && e.body.new_game && opts.onNewGame) opts.onNewGame(e.body);
        await loadGames();
        return false;
      } finally {
        delete st.busy[`game:${id}`];
        renderColumn("games");
      }
    }

    /**
     * The three-way choice of an id that exists already: copy (the default), overwrite, cancel.  The CURRENT game cannot be overwritten (the server
     * refuses with 409 ``current``): then the option is shown, off, with the reason - it is a copy or nothing.
     */
    async function askConflict(id, name) {
      const isLive = Boolean(id) && String(session().session_id || "") === id;
      const choice = await confirm({
        title: "对局 id 已存在",
        bodyHtml:
          `<div>文件 <strong>${esc(name)}</strong> 里的对局和已有的一局 id 相同（<code>${esc(id)}</code>）。${isLive ? "已有的这一局就是<strong>当前对局</strong>。" : ""}</div>` +
          `<label class="checkbox-label dialog-check"><input type="radio" name="game-conflict" id="radio-game-copy" value="copy" checked /><span>作为副本导入（推荐：两局都保留）</span></label>` +
          (isLive
            ? `<label class="checkbox-label dialog-check"><input type="radio" name="game-conflict" id="radio-game-overwrite" value="overwrite" disabled /><span>覆盖已有的那一局（当前对局不能被覆盖）</span></label>`
            : `<label class="checkbox-label dialog-check"><input type="radio" name="game-conflict" id="radio-game-overwrite" value="overwrite" /><span>覆盖已有的那一局（它的存档和日志会被替换）</span></label>`),
        okText: "导入",
        cancelText: "取消",
        collect: () => {
          // (the copy is the one that loses nothing: it is the answer unless the user took the other one)
          const copy = $("radio-game-copy");
          return { mode: !isLive && copy && copy.checked === false ? "overwrite" : "copy" };
        },
      });
      if (!choice || !choice.ok) return null;
      return (choice.values && choice.values.mode) || "copy";
    }

    async function importGame(file) {
      st.busy["games:import"] = true;
      st.games.notice = null;
      renderColumn("games");
      let mode = "ask";
      try {
        for (let attempt = 0; attempt < 2; attempt++) {
          try {
            const res = await api.upload(`api/games/import?name=${enc(file.name)}&on_conflict=${mode}`, file, "application/octet-stream");
            const id = res && res.session_id ? res.session_id : "";
            st.games.notice = {
              kind: "ok",
              text: res && res.copied ? `已作为副本导入：${id}` : res && res.replaced ? `已覆盖导入：${id}` : `已导入对局：${id}`,
            };
            showToast(st.games.notice.text);
            await loadGames();
            return true;
          } catch (e) {
            if (e && e.code === "exists" && mode === "ask") {
              const id = (e.body && e.body.session_id) || "";
              const answer = await askConflict(id, file.name);
              if (!answer) return false;
              mode = answer;
              continue;
            }
            st.games.notice = { kind: "error", text: errText(e) };
            showToast(errText(e));
            return false;
          }
        }
        return false;
      } finally {
        delete st.busy["games:import"];
        renderColumn("games");
      }
    }

    // -----------------------------------------------------------------------------------------------------------------
    // events
    // -----------------------------------------------------------------------------------------------------------------

    async function onPicked(pick, file) {
      switch (pick.kind) {
        case "core":
          return importCore(pick.botId, file);
        case "contract":
          return importContract(file);
        case "rules":
          return importRules(pick.id, file);
        case "material":
          return addMaterial(pick.id, file);
        case "game":
          return importGame(file);
        default:
          return false;
      }
    }

    function onClick(evt) {
      const node = closestFrom(evt && evt.target, "[data-settings-action]");
      if (!node || node.disabled) return;
      const attr = (k) => node.getAttribute(k);
      const action = attr("data-settings-action");
      const bot = attr("data-bot");
      const id = attr("data-id");
      switch (action) {
        case "toggle-col":
          setOpen(attr("data-col"), !st.open[attr("data-col")]);
          break;
        // ② 底层设定
        case "core-toggle":
          toggleCoreRow(bot);
          break;
        case "core-clear-all":
          clearCore(null);
          break;
        case "core-clear":
          clearCore([bot]);
          break;
        case "core-export":
          download(`api/bots/${enc(bot)}/core?download=1`);
          break;
        case "core-import":
          pickFile("core", ".md,.markdown,text/markdown,text/plain", { botId: bot });
          break;
        case "core-gen":
        case "core-default": {
          // the editors of the panel have the notes box and the draft that is read first (生成) and the default library (套用): go there
          if (archive && typeof archive.closeSettings === "function") archive.closeSettings();
          if (win.Bots && typeof win.Bots.focus === "function") win.Bots.focus(bot, action === "core-gen" ? "edit_core" : "import_default");
          break;
        }
        // ③ 官方契约
        case "contract-toggle":
          st.contract.preview = !st.contract.preview;
          renderColumn("contract");
          break;
        case "contract-export":
          download("api/contract?download=1");
          break;
        case "contract-import":
          pickFile("contract", ".md,.markdown,text/markdown,text/plain");
          break;
        case "contract-reset":
          resetContract();
          break;
        case "contract-refresh":
          loadContract();
          break;
        // ④ 规则
        case "rules-export":
          download(`api/rules/${enc(id)}?download=1`);
          break;
        case "rules-import":
          pickFile("rules", ".md,.markdown,text/markdown,text/plain", { id });
          break;
        case "rules-reset":
          resetRules(id);
          break;
        case "rules-refresh":
          loadRules();
          break;
        case "mat-export":
          download(`api/rules/${enc(id)}/materials/${enc(attr("data-slug"))}?download=1`);
          break;
        case "mat-add":
          pickFile("material", ".md,.markdown,.txt,text/markdown,text/plain", { id });
          break;
        case "mat-delete":
          deleteMaterial(id, attr("data-slug"));
          break;
        // ⑤ 对局管理
        case "games-refresh":
          loadGames();
          break;
        case "games-toggle":
          st.games.expanded[id] = !st.games.expanded[id];
          if (st.games.expanded[id] && !st.games.detail[id]) loadGameDetail(id);
          else renderColumn("games");
          break;
        case "games-export":
          exportGame(id);
          break;
        case "games-delete":
          deleteGame(id);
          break;
        case "games-import":
          pickFile("game", ".arenagame,application/octet-stream");
          break;
        case "games-history":
          // the history dialog (replay / continue) - this one steps aside for it
          if (archive && typeof archive.closeSettings === "function") archive.closeSettings();
          if (typeof opts.openHistory === "function") opts.openHistory(id || "");
          break;
        default:
          break;
      }
    }

    on(el.body, "click", onClick);
    on(el.fileInput, "change", (evt) => {
      const file = evt && evt.target && evt.target.files && evt.target.files[0];
      const pick = st.pick;
      if (evt && evt.target) evt.target.value = "";
      st.pick = null;
      if (!file || !pick) return;
      onPicked(pick, file);
    });

    return {
      wrapHome,
      onHomeRendered,
      onOpened,
      open,
      isOpen,
      /** The bots changed (an initialization, a new L0, a clear): the column of the L0s follows. */
      onBots() {
        if (!isOpen()) return;
        renderColumn("core");
        renderMeta();
      },
      /** The live game changed (a status, a new game): the games' buttons follow. */
      onSession() {
        if (!isOpen()) return;
        if (st.games.data) renderColumn("games");
        renderMeta();
      },
      refresh(column) {
        if (column === "contract") return loadContract();
        if (column === "rules") return loadRules();
        if (column === "games") return loadGames();
        return Promise.resolve();
      },
      columns: () => COLUMN_IDS.slice(),
      openState: () => ({ ...st.open }),
      _state: st,
    };
  }

  root.ArenaSettingsUI = {
    create,
    COLUMNS,
    OPEN_KEY,
    STALE_SENTENCE,
    CLEAR_CORE_SENTENCE,
    DELETE_GAME_SENTENCE,
    DELETE_GAME_NOTE,
    COLUMN_ALIASES,
    columnOf,
    parseOpen,
    formatSize,
    shortHash,
    slugOfFileName,
    formatWhen,
    gameRow,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = root.ArenaSettingsUI;
})(typeof window !== "undefined" ? window : globalThis);
