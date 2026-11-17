/* ==========================================================================
   Arena-Agents v0.12 Frontend Core (pure helpers, no DOM access)
   - Loaded before app.js in the browser (exposes window.ArenaCore)
   - Also loadable from Node (module.exports) so tests/frontend/*.js can
     exercise it without a browser.
   - v0.12: card-size tiers are gone (players are tiny fixed-size tiles);
     buildEventsUrl() can carry last_event_id for SSE v2 resume; number /
     time formatting helpers live here.
   ========================================================================== */

(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.ArenaCore = api;
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // -------------------------------------------------------------------------
  // Escaping & numbers
  // -------------------------------------------------------------------------

  function escapeHtml(str) {
    if (str === null || str === undefined) return "";
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  /** Coerce any value to a finite number (fallback when not finite). */
  function safeNum(value, fallback = 0) {
    const n = typeof value === "number" ? value : Number(value);
    return Number.isFinite(n) ? n : fallback;
  }

  /** Number rendered as escaped text, safe to splice into HTML. */
  function numText(value, fallback = 0) {
    return escapeHtml(String(safeNum(value, fallback)));
  }

  // -------------------------------------------------------------------------
  // Markdown-ish formatting (escape first, then wrap)
  // -------------------------------------------------------------------------

  function stripMarkdownForPreview(str) {
    if (!str) return "";
    return String(str)
      .replace(/```[\s\S]*?```/g, " [代码] ")
      .replace(/`([^`]+)`/g, "$1")
      .replace(/\*\*([^*]+)\*\*/g, "$1")
      .replace(/__([^_]+)__/g, "$1")
      .replace(/(?:^|\s)#{1,6}\s+/g, " ")
      .replace(/\s+/g, " ")
      .trim();
  }

  function renderInlineFormatting(rawStr) {
    if (!rawStr) return "";
    const codePlaceholders = [];
    let text = String(rawStr).replace(/`([^`\n]+)`/g, (_, codeContent) => {
      const idx = codePlaceholders.length;
      codePlaceholders.push(
        `<code class="fmt-code">${escapeHtml(codeContent)}</code>`
      );
      return `\u0000CODE_${idx}\u0000`;
    });

    text = escapeHtml(text);

    // Bold: **text** or __text__
    text = text.replace(
      /\*\*([\s\S]+?)\*\*/g,
      '<strong class="fmt-strong">$1</strong>'
    );
    text = text.replace(
      /__([\s\S]+?)__/g,
      '<strong class="fmt-strong">$1</strong>'
    );

    // Italic: *text* (not adjacent to *)
    text = text.replace(
      /(^|[^*])\*([^*\n]+)\*(?!\*)/g,
      '$1<em class="fmt-em">$2</em>'
    );

    // Restore inline code placeholders
    text = text.replace(/\u0000CODE_(\d+)\u0000/g, (_, idx) => {
      return codePlaceholders[Number(idx)] || "";
    });

    return text;
  }

  /**
   * Render text into an array of top-level HTML blocks (each block is exactly
   * one element). Returning blocks (instead of one string) lets the streaming
   * view patch only the blocks that changed.
   */
  function renderFormattedBlocks(rawText) {
    if (rawText === null || rawText === undefined) return [];
    // NUL is used internally for placeholders; never let user text forge it.
    let src = String(rawText)
      .replace(/\u0000/g, "")
      .replace(/\r\n/g, "\n")
      .replace(/\r/g, "\n");
    if (!src.trim()) return [];

    // Extract fenced code blocks ```...```
    const blockCodePlaceholders = [];
    src = src.replace(/```(?:[a-zA-Z0-9_-]+)?\n?([\s\S]*?)```/g, (_, code) => {
      const idx = blockCodePlaceholders.length;
      blockCodePlaceholders.push(
        `<pre class="fmt-pre"><code>${escapeHtml(code.trim())}</code></pre>`
      );
      return `\n\u0000BLOCKCODE_${idx}\u0000\n`;
    });

    const lines = src.split("\n");
    const htmlBlocks = [];
    let paraLines = [];
    let listType = null; // "ul" | "ol"
    let listItems = [];
    let quoteLines = [];

    const flushPara = () => {
      if (paraLines.length > 0) {
        htmlBlocks.push(`<p class="fmt-p">${paraLines.join("<br/>")}</p>`);
        paraLines = [];
      }
    };

    const flushList = () => {
      if (listItems.length > 0 && listType) {
        const tag = listType;
        const cls = listType === "ol" ? "fmt-ol" : "fmt-ul";
        htmlBlocks.push(`<${tag} class="${cls}">${listItems.join("")}</${tag}>`);
        listItems = [];
        listType = null;
      }
    };

    const flushQuote = () => {
      if (quoteLines.length > 0) {
        htmlBlocks.push(
          `<blockquote class="fmt-quote">${quoteLines.join("<br/>")}</blockquote>`
        );
        quoteLines = [];
      }
    };

    const flushAll = () => {
      flushPara();
      flushList();
      flushQuote();
    };

    for (let i = 0; i < lines.length; i++) {
      const rawLine = lines[i];
      const trimmed = rawLine.trim();

      if (!trimmed) {
        flushAll();
        continue;
      }

      const codeMatch = trimmed.match(/^\u0000BLOCKCODE_(\d+)\u0000$/);
      if (codeMatch) {
        flushAll();
        htmlBlocks.push(blockCodePlaceholders[Number(codeMatch[1])] || "");
        continue;
      }

      if (/^(\*{3,}|-{3,}|_{3,})$/.test(trimmed)) {
        flushAll();
        htmlBlocks.push('<hr class="fmt-hr" />');
        continue;
      }

      const headingMatch = trimmed.match(/^(#{1,4})\s+(.+)$/);
      if (headingMatch) {
        flushAll();
        const level = headingMatch[1].length;
        htmlBlocks.push(
          `<div class="fmt-heading fmt-h${level}">${renderInlineFormatting(
            headingMatch[2]
          )}</div>`
        );
        continue;
      }

      const quoteMatch = trimmed.match(/^>\s?(.*)$/);
      if (quoteMatch) {
        flushPara();
        flushList();
        quoteLines.push(renderInlineFormatting(quoteMatch[1]));
        continue;
      } else if (quoteLines.length > 0) {
        flushQuote();
      }

      const ulMatch = rawLine.match(/^\s*[-*•]\s+(.+)$/);
      if (ulMatch) {
        flushPara();
        if (listType && listType !== "ul") flushList();
        listType = "ul";
        listItems.push(
          `<li class="fmt-li">${renderInlineFormatting(ulMatch[1].trim())}</li>`
        );
        continue;
      }

      const olMatch = rawLine.match(/^\s*(\d+)[.)、]\s+(.+)$/);
      if (olMatch) {
        flushPara();
        if (listType && listType !== "ol") flushList();
        listType = "ol";
        listItems.push(
          `<li class="fmt-li" value="${escapeHtml(
            olMatch[1]
          )}">${renderInlineFormatting(olMatch[2].trim())}</li>`
        );
        continue;
      }

      if (listType) {
        flushList();
      }
      paraLines.push(renderInlineFormatting(trimmed));
    }

    flushAll();
    return htmlBlocks.filter(Boolean);
  }

  function renderFormattedText(rawText) {
    return renderFormattedBlocks(rawText).join("");
  }

  // -------------------------------------------------------------------------
  // Bounded Map (expand/collapse state must not grow forever)
  // -------------------------------------------------------------------------

  /** Set key on a Map, refreshing its recency and evicting the oldest entries. */
  function boundedSet(map, key, value, max = 2000) {
    if (map.has(key)) map.delete(key);
    map.set(key, value);
    while (map.size > max) {
      const oldest = map.keys().next().value;
      map.delete(oldest);
    }
    return map;
  }

  // -------------------------------------------------------------------------
  // URL helpers (sub-path reverse-proxy friendly)
  // -------------------------------------------------------------------------

  /**
   * Derive the URL prefix the app is served under from the script's own URL
   * ("/arena/static/js/app.js" -> "/arena/"). Falls back to "/".
   */
  function prefixFromScriptPath(scriptPathname) {
    const p = String(scriptPathname || "");
    const m = p.match(/^(.*\/)static\/js\/[^/]*$/);
    if (m) return m[1];
    return "/";
  }

  function joinUrl(prefix, path) {
    const base = prefix || "";
    const rel = String(path || "").replace(/^\/+/, "");
    return base + rel;
  }

  /**
   * SSE v2 URL. A manually re-created EventSource does not send the browser's
   * automatic Last-Event-ID header, so the resume point travels as the
   * ?last_event_id= query parameter (the server accepts both).
   */
  function buildEventsUrl(prefix, token, lastEventId) {
    const url = joinUrl(prefix, "api/events");
    const params = [];
    if (token) params.push(`token=${encodeURIComponent(token)}`);
    const id = Number(lastEventId);
    if (
      lastEventId !== undefined &&
      lastEventId !== null &&
      lastEventId !== "" &&
      Number.isFinite(id) &&
      id > 0
    ) {
      params.push(`last_event_id=${Math.floor(id)}`);
    }
    return params.length ? `${url}?${params.join("&")}` : url;
  }

  // -------------------------------------------------------------------------
  // Reconnect backoff
  // -------------------------------------------------------------------------

  /**
   * Exponential backoff with jitter. attempt starts at 0. The result never
   * exceeds capMs. jitter=0.3 means the delay is in [0.7*d, d].
   */
  function computeBackoff(attempt, opts = {}) {
    const baseMs = opts.baseMs ?? 1000;
    const capMs = opts.capMs ?? 30000;
    const jitter = opts.jitter ?? 0.3;
    const rand = opts.rand || Math.random;
    const n = Math.max(0, Math.floor(safeNum(attempt, 0)));
    const exp = Math.min(capMs, baseMs * Math.pow(2, Math.min(n, 30)));
    return Math.max(0, Math.round(exp * (1 - jitter * rand())));
  }

  // -------------------------------------------------------------------------
  // API error handling
  // -------------------------------------------------------------------------

  class ApiError extends Error {
    constructor(message, info = {}) {
      super(message);
      this.name = "ApiError";
      this.status = info.status || 0;
      this.detail = info.detail || "";
      this.kind = info.kind || "http"; // http | network | bad-response | auth
    }
  }

  /** Turn any FastAPI-ish "detail" into a readable string (never "[object Object]"). */
  function formatDetail(detail, depth = 0) {
    if (detail === null || detail === undefined) return "";
    if (typeof detail === "string") return detail;
    if (typeof detail === "number" || typeof detail === "boolean") {
      return String(detail);
    }
    if (Array.isArray(detail)) {
      return detail
        .map((x) => formatDetail(x, depth + 1))
        .filter(Boolean)
        .join("；");
    }
    if (typeof detail === "object") {
      const inner =
        detail.msg ?? detail.message ?? detail.detail ?? detail.error ?? null;
      if (inner !== null && depth < 3) {
        const msg = formatDetail(inner, depth + 1);
        const loc = Array.isArray(detail.loc)
          ? detail.loc.filter((x) => x !== "body").join(".")
          : "";
        return loc ? `${loc}: ${msg}` : msg;
      }
      try {
        const j = JSON.stringify(detail);
        return j === undefined ? "" : j;
      } catch {
        return "";
      }
    }
    return "";
  }

  const STATUS_LABELS = {
    400: "请求无效",
    401: "需要有效的访问令牌",
    403: "请求被拒绝（来源或权限不允许）",
    404: "接口或资源不存在",
    405: "请求方法不被允许",
    409: "操作冲突（当前对局状态不允许）",
    413: "请求内容过大",
    422: "参数校验失败",
    429: "请求过于频繁，请稍后再试",
  };

  function describeHttpError(status, detail) {
    const d = formatDetail(detail).slice(0, 300);
    let label = STATUS_LABELS[status];
    if (!label) {
      label = status >= 500 ? `服务器内部错误 (${status})` : `请求失败 (${status})`;
    } else if (status >= 500) {
      label = `服务器内部错误 (${status})`;
    }
    return d ? `${label}：${d}` : label;
  }

  function looksJson(text) {
    const t = String(text || "").trimStart();
    return t.startsWith("{") || t.startsWith("[");
  }

  /** File name out of a Content-Disposition header (filename*=UTF-8''… wins); "" when absent. */
  function filenameFromDisposition(header) {
    const h = String(header || "");
    let m = /filename\*\s*=\s*(?:UTF-8|utf-8)''([^;]+)/.exec(h);
    if (m) {
      try {
        return decodeURIComponent(m[1].trim());
      } catch {
        /* fall through to the plain form */
      }
    }
    m = /filename\s*=\s*"?([^";]+)"?/.exec(h);
    return m ? m[1].trim() : "";
  }

  /** Read a Response body without ever throwing on malformed/non-JSON content. */
  async function readBody(resp) {
    let ct = "";
    try {
      ct = String((resp.headers && resp.headers.get("content-type")) || "").toLowerCase();
    } catch {
      ct = "";
    }
    let text = "";
    try {
      text = await resp.text();
    } catch {
      text = "";
    }
    if (/json/.test(ct) || (!ct && looksJson(text))) {
      try {
        return { kind: "json", data: JSON.parse(text), text };
      } catch {
        return { kind: "invalid-json", data: null, text };
      }
    }
    return { kind: "text", data: null, text };
  }

  /** Best-effort short, human-readable detail for an error response. */
  function extractErrorDetail(body) {
    if (body.kind === "json") {
      const d = body.data;
      if (d && typeof d === "object" && !Array.isArray(d) && "detail" in d) {
        return formatDetail(d.detail);
      }
      return formatDetail(d);
    }
    const t = String(body.text || "").trim();
    // Never dump HTML error pages or huge bodies into a toast.
    if (!t || t.startsWith("<") || t.length > 200) return "";
    return t;
  }

  /**
   * Build an API client.
   *  fetchImpl:      fetch-compatible function
   *  getToken():     current token string ("" when none)
   *  onAuthRequired({hadToken, usedToken, detail}) -> Promise<string|null>
   *                  called on 401; resolve a (new) token to retry, or null to give up
   *  prefix:         URL prefix, e.g. "/" or "/arena/"
   */
  function createApiClient(opts) {
    const fetchImpl = opts.fetchImpl;
    const getToken = opts.getToken || (() => "");
    const onAuthRequired = opts.onAuthRequired || null;
    const prefix = opts.prefix || "";
    const maxAuthAttempts = opts.maxAuthAttempts ?? 5;

    /**
     * options: {method, body (JSON), headers,
     *           rawBody + contentType (send bytes as they are, e.g. a zip),
     *           responseType: "blob" (a successful response is returned as {blob, filename})}
     */
    async function request(path, options = {}) {
      const method = options.method || "GET";
      const url = joinUrl(prefix, path);
      let body;
      let contentType = "";
      if (options.rawBody !== undefined && options.rawBody !== null) {
        body = options.rawBody;
        contentType = options.contentType || "application/octet-stream";
      } else if (options.body !== undefined && options.body !== null) {
        body = JSON.stringify(options.body);
        contentType = "application/json";
      }

      for (let attempt = 0; ; attempt++) {
        const headers = { ...(options.headers || {}) };
        if (body !== undefined) headers["Content-Type"] = contentType;
        const usedToken = getToken();
        if (usedToken) headers["X-Arena-Token"] = usedToken;

        let resp;
        try {
          resp = await fetchImpl(url, { method, headers, body });
        } catch (e) {
          throw new ApiError("无法连接到服务器，请检查网络或服务是否在运行", {
            kind: "network",
          });
        }

        if (options.responseType === "blob" && resp.ok && typeof resp.blob === "function") {
          const blob = await resp.blob();
          return { blob, filename: filenameFromDisposition(resp.headers && resp.headers.get && resp.headers.get("content-disposition")) };
        }

        const parsed = await readBody(resp);

        if (resp.status === 401 && onAuthRequired && attempt < maxAuthAttempts) {
          const detail = extractErrorDetail(parsed);
          const tok = await onAuthRequired({
            hadToken: Boolean(usedToken),
            usedToken,
            detail,
          });
          if (tok) continue;
          throw new ApiError(describeHttpError(401, detail), {
            status: 401,
            detail,
            kind: "auth",
          });
        }

        if (!resp.ok) {
          const detail = extractErrorDetail(parsed);
          throw new ApiError(describeHttpError(resp.status, detail), {
            status: resp.status,
            detail,
            kind: "http",
          });
        }

        if (resp.status === 204 || !String(parsed.text || "").trim()) return null;
        if (parsed.kind !== "json") {
          throw new ApiError("服务器返回了非 JSON 响应（可能被代理或网关拦截）", {
            status: resp.status,
            kind: "bad-response",
          });
        }
        return parsed.data;
      }
    }

    return {
      request,
      get: (path, options) => request(path, { ...(options || {}), method: "GET" }),
      post: (path, body, options) =>
        request(path, { ...(options || {}), method: "POST", body }),
      // v0.12.1: raw bytes (a zip) and binary downloads
      upload: (path, rawBody, contentType, options) =>
        request(path, { ...(options || {}), method: "POST", rawBody, contentType }),
      download: (path, options) =>
        request(path, { ...(options || {}), method: "GET", responseType: "blob" }),
      put: (path, body, options) =>
        request(path, { ...(options || {}), method: "PUT", body }),
      patch: (path, body, options) =>
        request(path, { ...(options || {}), method: "PATCH", body }),
      // DELETE with a JSON body (model ids may contain slashes, so they travel in the body).
      del: (path, body, options) =>
        request(path, { ...(options || {}), method: "DELETE", body }),
    };
  }

  // -------------------------------------------------------------------------
  // v0.10: providers / models / bindings (pure helpers)
  // -------------------------------------------------------------------------

  const PROTOCOLS = [
    { id: "openai_chat", label: "OpenAI 对话补全（Chat Completions）" },
    { id: "openai_responses", label: "OpenAI 响应接口（Responses）" },
    { id: "anthropic_messages", label: "Anthropic 消息接口（Messages）" },
  ];

  const REASONING_DEPTHS = [
    { id: "low", label: "low（浅）" },
    { id: "medium", label: "medium（中）" },
    { id: "high", label: "high（深）" },
    { id: "xhigh", label: "xhigh（极深）" },
  ];

  function protocolLabel(id) {
    const p = PROTOCOLS.find((x) => x.id === id);
    return p ? p.label : id ? String(id) : "未知协议";
  }

  /** Accept [..] or {providers:[..]}; always return an array of objects. */
  function normalizeProviders(resp) {
    const list = Array.isArray(resp)
      ? resp
      : resp && Array.isArray(resp.providers)
        ? resp.providers
        : [];
    return list
      .filter((p) => p && typeof p === "object" && p.id)
      .map((p) => ({
        ...p,
        // v0.11: absent/legacy providers mean "auto-append the API path" (v0.10 behaviour).
        auto_append_path: p.auto_append_path !== false,
        models: Array.isArray(p.models) ? p.models.filter((m) => m && m.id) : [],
      }));
  }

  /** Accept ["a"], [{id:"a"}], {models:[..]}; return unique non-empty strings. */
  function normalizeModelIds(resp) {
    const list = Array.isArray(resp) ? resp : resp && Array.isArray(resp.models) ? resp.models : [];
    const out = [];
    const seen = new Set();
    for (const m of list) {
      const id = typeof m === "string" ? m : m && typeof m === "object" ? m.id : "";
      if (typeof id === "string" && id.trim() && !seen.has(id)) {
        seen.add(id);
        out.push(id);
      }
    }
    return out;
  }

  function isBlank(v) {
    return v === null || v === undefined || String(v).trim() === "";
  }

  /** Price is REQUIRED: empty is an error (never silently 0); finite and >= 0. */
  function validatePrice(raw, label = "价格") {
    if (isBlank(raw)) return { ok: false, error: `${label}为必填项` };
    const s = String(raw).trim();
    if (!/^[+]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$/.test(s)) {
      return { ok: false, error: `${label}必须是非负数字` };
    }
    const n = Number(s);
    if (!Number.isFinite(n) || n < 0) {
      return { ok: false, error: `${label}必须是有限的非负数字` };
    }
    return { ok: true, value: n };
  }

  /** Optional integer context window (empty => not provided). */
  function validateContextWindow(raw) {
    if (isBlank(raw)) return { ok: true, value: undefined };
    const s = String(raw).trim();
    if (!/^\d+$/.test(s)) return { ok: false, error: "上下文窗口必须是正整数" };
    const n = Number(s);
    if (!Number.isSafeInteger(n) || n < 2000) {
      return { ok: false, error: "上下文窗口不能小于 2000 Tokens" };
    }
    return { ok: true, value: n };
  }

  function hasControlChars(s) {
    return /[\u0000-\u001f\u007f]/.test(String(s));
  }

  function validateBaseUrl(raw) {
    const s = String(raw === undefined || raw === null ? "" : raw).trim();
    if (!s) return { ok: false, error: "Base URL 为必填项" };
    if (hasControlChars(s)) return { ok: false, error: "Base URL 含有非法控制字符" };
    let u;
    try {
      u = new URL(s);
    } catch {
      return { ok: false, error: "Base URL 不是有效的 URL" };
    }
    if (u.protocol !== "http:" && u.protocol !== "https:") {
      return { ok: false, error: "Base URL 必须以 http:// 或 https:// 开头" };
    }
    if (u.username || u.password) {
      return { ok: false, error: "Base URL 不能包含用户名或密码" };
    }
    return { ok: true, value: s };
  }

  /**
   * Build the body for POST /providers (existing=null) or PATCH /providers/{id}.
   * Editing: only changed fields are sent; an empty key means "unchanged", but
   * changing the endpoint (protocol / base_url / auto_append_path) REQUIRES a
   * (new) key: the stored key would be sent to a different address.
   */
  function buildProviderPayload(form, existing) {
    const errors = [];
    const name = String(form.name || "").trim();
    const protocol = String(form.protocol || "");
    const apiKey = String(form.api_key || "").trim();
    const baseUrlRes = validateBaseUrl(form.base_url);
    const autoAppend = form.auto_append_path === undefined ? true : form.auto_append_path !== false;

    if (!name) errors.push("名称为必填项");
    else if (hasControlChars(name)) errors.push("名称含有非法控制字符");
    if (!PROTOCOLS.some((p) => p.id === protocol)) errors.push("请选择协议");
    if (!baseUrlRes.ok) errors.push(baseUrlRes.error);
    if (apiKey && hasControlChars(apiKey)) errors.push("API Key 含有非法控制字符");

    if (!existing) {
      if (!apiKey) errors.push("API Key 为必填项");
      if (errors.length) return { ok: false, errors };
      return {
        ok: true,
        payload: {
          name,
          protocol,
          base_url: baseUrlRes.value,
          api_key: apiKey,
          auto_append_path: autoAppend,
        },
      };
    }

    const payload = {};
    if (name && name !== existing.name) payload.name = name;
    const protocolChanged = protocol !== existing.protocol;
    const baseChanged = baseUrlRes.ok && baseUrlRes.value !== existing.base_url;
    const autoChanged = autoAppend !== providerAutoAppend(existing);
    if (protocolChanged) payload.protocol = protocol;
    if (baseChanged) payload.base_url = baseUrlRes.value;
    if (autoChanged) payload.auto_append_path = autoAppend;
    if ((protocolChanged || baseChanged || autoChanged) && !apiKey) {
      errors.push("修改协议、Base URL 或自动拼接路径时必须重新输入 API Key");
    }
    if (apiKey) payload.api_key = apiKey;
    if (errors.length) return { ok: false, errors };
    return { ok: true, payload };
  }

  /** v0.11: providers stored before auto_append_path existed keep the v0.10 behaviour. */
  function providerAutoAppend(provider) {
    return !(provider && provider.auto_append_path === false);
  }

  /** "add" when the model id is new for this provider, otherwise "edit". */
  function modelFormMode(provider, modelId) {
    const id = String(modelId || "");
    const exists = Boolean(provider && (provider.models || []).some((m) => m.id === id));
    return exists ? "edit" : "add";
  }

  /** Optional price (the two cache prices): empty means "bill at the input price" => null. */
  function validateOptionalPrice(raw, label = "价格") {
    if (isBlank(raw)) return { ok: true, value: null };
    return validatePrice(raw, label);
  }

  /**
   * Body for POST (add) / PATCH (edit) of a single model (v0.12.1: four prices, $ per 1M tokens).
   * input (cache miss) and output are mandatory; cache read / write may be empty (null = input price).
   */
  function buildModelPayload(form) {
    const errors = [];
    const modelId = String(form.model_id || "").trim();
    if (!modelId) errors.push("请选择或填写一个模型");
    else if (hasControlChars(modelId)) errors.push("模型 ID 含有非法控制字符");
    const pin = validatePrice(form.input_price_per_m, "输入价格");
    const pout = validatePrice(form.output_price_per_m, "输出价格");
    const pcr = validateOptionalPrice(form.cache_read_price_per_m, "缓存读取价格");
    const pcw = validateOptionalPrice(form.cache_write_price_per_m, "缓存写入价格");
    if (!pin.ok) errors.push(pin.error);
    if (!pout.ok) errors.push(pout.error);
    if (!pcr.ok) errors.push(pcr.error);
    if (!pcw.ok) errors.push(pcw.error);
    const ctx = validateContextWindow(form.context_window_tokens);
    if (!ctx.ok) errors.push(ctx.error);
    if (errors.length) return { ok: false, errors };
    const payload = {
      model_id: modelId,
      input_price_per_m: pin.value,
      cache_read_price_per_m: pcr.value,
      cache_write_price_per_m: pcw.value,
      output_price_per_m: pout.value,
    };
    if (ctx.value !== undefined) payload.context_window_tokens = ctx.value;
    return { ok: true, payload };
  }

  // -------------------------------------------------------------------------
  // v0.12.1: price source (E12) and per-model price suggestions
  // -------------------------------------------------------------------------

  const DEFAULT_PRICING_SOURCE_URL =
    "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json";

  /** Local "YYYY-MM-DD HH:MM" of an ISO string / epoch seconds / epoch milliseconds ("" when unusable). */
  function formatDateTime(value) {
    if (value === undefined || value === null || value === "") return "";
    let d;
    if (typeof value === "number" || /^\d+(\.\d+)?$/.test(String(value))) {
      const n = Number(value);
      d = new Date(n < 1e11 ? n * 1000 : n);
    } else {
      d = new Date(value);
    }
    if (Number.isNaN(d.getTime())) return "";
    const p = (x) => String(x).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  /** GET/PUT /api/pricing/source and POST /api/pricing/refresh -> {url, default_url, fetched_at, entries, error}. */
  function normalizePricingSource(resp) {
    const r = resp && typeof resp === "object" && !Array.isArray(resp) ? resp : {};
    const entries = Number(r.entries);
    const err = r.error === undefined || r.error === null || r.error === "" ? null : formatDetail(r.error);
    return {
      url: typeof r.url === "string" ? r.url : "",
      default_url:
        typeof r.default_url === "string" && r.default_url.trim() ? r.default_url : DEFAULT_PRICING_SOURCE_URL,
      fetched_at: r.fetched_at === undefined || r.fetched_at === null || r.fetched_at === "" ? null : r.fetched_at,
      entries: Number.isFinite(entries) && entries >= 0 ? Math.floor(entries) : 0,
      error: err,
    };
  }

  /** The source must be an https URL (the server enforces it again, and refuses internal addresses). */
  function validatePricingSourceUrl(raw) {
    const s = String(raw === undefined || raw === null ? "" : raw).trim();
    if (!s) return { ok: false, error: "价格源 URL 不能为空" };
    if (hasControlChars(s)) return { ok: false, error: "价格源 URL 含有非法控制字符" };
    let u;
    try {
      u = new URL(s);
    } catch {
      return { ok: false, error: "价格源 URL 不是有效的 URL" };
    }
    if (u.protocol !== "https:") return { ok: false, error: "价格源 URL 必须以 https:// 开头" };
    if (u.username || u.password) return { ok: false, error: "价格源 URL 不能包含用户名或密码" };
    return { ok: true, value: s };
  }

  /** One line for the dialog: "已加载 2,345 条 · 获取于 2026-11-17 05:20" / the error / "尚未获取". */
  function pricingSourceStatus(info) {
    const i = normalizePricingSource(info);
    const when = formatDateTime(i.fetched_at);
    if (i.error) {
      return {
        kind: "error",
        text: `获取失败：${i.error}${i.entries ? `（仍使用上次的 ${formatCount(i.entries)} 条${when ? `，${when}` : ""}）` : ""}`,
      };
    }
    if (i.entries > 0) {
      return { kind: "ok", text: `已加载 ${i.entries.toLocaleString("en-US")} 条${when ? ` · 获取于 ${when}` : ""}` };
    }
    return { kind: "none", text: "尚未获取价格表：点击「刷新价格表」" };
  }

  /** A `suggested` object of the model-discovery response / pricing match -> null | numbers (or null) per field. */
  function normalizeSuggestion(raw) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
    if (raw.matched === false) return null;
    const pick = (k) => {
      const v = raw[k];
      if (v === undefined || v === null || v === "") return null;
      const n = Number(v);
      return Number.isFinite(n) && n >= 0 ? n : null;
    };
    const out = {
      input_price_per_m: pick("input_price_per_m"),
      output_price_per_m: pick("output_price_per_m"),
      cache_read_price_per_m: pick("cache_read_price_per_m"),
      cache_write_price_per_m: pick("cache_write_price_per_m"),
      context_window_tokens: pick("context_window_tokens"),
      key: typeof raw.key === "string" && raw.key ? raw.key : typeof raw.price_source === "string" ? raw.price_source : "",
    };
    // a suggestion without any price is "no match"
    return out.input_price_per_m === null && out.output_price_per_m === null ? null : out;
  }

  /**
   * Suggestions out of the discover-models response. Both shapes are accepted:
   *   {models: [{id, suggested}, "plain-id"]}   and   {models: [...], suggested: {"<id>": {...}}}
   * Returns {ids, suggestions: {id: suggestion|null}}.
   */
  function parseDiscoveredModels(resp) {
    const ids = normalizeModelIds(resp);
    const suggestions = {};
    const list = Array.isArray(resp) ? resp : resp && Array.isArray(resp.models) ? resp.models : [];
    for (const m of list) {
      if (m && typeof m === "object" && m.id !== undefined) {
        suggestions[String(m.id)] = normalizeSuggestion(m.suggested);
      }
    }
    const map = resp && typeof resp === "object" && resp.suggested && typeof resp.suggested === "object" ? resp.suggested : null;
    if (map && !Array.isArray(map)) {
      for (const id of Object.keys(map)) suggestions[id] = normalizeSuggestion(map[id]);
    }
    for (const id of ids) if (!(id in suggestions)) suggestions[id] = null;
    return { ids, suggestions };
  }

  /** Input values (strings) for the model form out of a suggestion. */
  function suggestionToForm(s) {
    const str = (v) => (v === null || v === undefined ? "" : String(v));
    if (!s) {
      return { input: "", cacheRead: "", cacheWrite: "", output: "", ctx: "" };
    }
    return {
      input: str(s.input_price_per_m),
      cacheRead: str(s.cache_read_price_per_m),
      cacheWrite: str(s.cache_write_price_per_m),
      output: str(s.output_price_per_m),
      ctx: str(s.context_window_tokens),
    };
  }

  /** "$0.27" / "—" for a price per million tokens (null/undefined => not set). */
  function formatPrice(v) {
    if (v === null || v === undefined || v === "") return "—";
    const n = Number(v);
    if (!Number.isFinite(n)) return "—";
    return `$${n >= 100 ? n.toFixed(0) : n >= 1 ? n.toFixed(2).replace(/\.?0+$/, "") : String(Number(n.toPrecision(3)))}`;
  }

  // -------------------------------------------------------------------------
  // v0.12.1: token / cost budget (E10)
  // -------------------------------------------------------------------------

  const BUDGET_WARN_RATIO = 0.8;

  /** GET/PUT /api/budget and PublicSession.budget -> a complete budget object. */
  function normalizeBudget(raw) {
    const b = raw && typeof raw === "object" && !Array.isArray(raw) ? raw : {};
    const posInt = (v) => {
      if (v === undefined || v === null || v === "") return null;
      const n = Number(v);
      return Number.isFinite(n) && n > 0 ? Math.floor(n) : null;
    };
    const posNum = (v) => {
      if (v === undefined || v === null || v === "") return null;
      const n = Number(v);
      return Number.isFinite(n) && n > 0 ? n : null;
    };
    const used = (v) => {
      const n = Number(v);
      return Number.isFinite(n) && n >= 0 ? n : 0;
    };
    const state = b.state === "warning" || b.state === "reached" ? b.state : "ok";
    return {
      max_tokens: posInt(b.max_tokens),
      max_cost_usd: posNum(b.max_cost_usd),
      force_stop: Boolean(b.force_stop),
      used_tokens: used(b.used_tokens),
      used_cost_usd: used(b.used_cost_usd),
      state,
    };
  }

  /** "ok" | "warning" (>= 80 %) | "reached" (>= 100 %) for the usage against the limits. */
  function budgetStateOf(budget) {
    const b = normalizeBudget(budget);
    const ratio = budgetRatio(b);
    if (ratio === null) return "ok";
    return ratio >= 1 ? "reached" : ratio >= BUDGET_WARN_RATIO ? "warning" : "ok";
  }

  /** Highest used/limit ratio over the limits that are set (null when there is no limit at all). */
  function budgetRatio(budget) {
    const b = normalizeBudget(budget);
    const ratios = [];
    if (b.max_tokens) ratios.push(b.used_tokens / b.max_tokens);
    if (b.max_cost_usd) ratios.push(b.used_cost_usd / b.max_cost_usd);
    return ratios.length ? Math.max(...ratios) : null;
  }

  /** One input of the budget dialog: "" => no limit (null); otherwise a positive number. */
  function parseBudgetField(raw, kind) {
    if (isBlank(raw)) return { ok: true, value: null };
    const s = String(raw).trim().replace(/[,_\s]/g, "");
    const label = kind === "cost" ? "费用上限" : "token 上限";
    if (!/^[+]?(\d+\.?\d*|\.\d+)$/.test(s)) return { ok: false, error: `${label}必须是正数（留空表示不限）` };
    const n = Number(s);
    if (!Number.isFinite(n) || n <= 0) return { ok: false, error: `${label}必须大于 0（留空表示不限）` };
    if (kind === "tokens") {
      if (!Number.isInteger(n)) return { ok: false, error: "token 上限必须是整数" };
      if (!Number.isSafeInteger(n)) return { ok: false, error: "token 上限太大" };
    }
    return { ok: true, value: n };
  }

  /** Body for PUT /api/budget out of the dialog's fields. */
  function buildBudgetPayload(form) {
    const f = form || {};
    const errors = [];
    const tokens = parseBudgetField(f.max_tokens, "tokens");
    const cost = parseBudgetField(f.max_cost_usd, "cost");
    if (!tokens.ok) errors.push(tokens.error);
    if (!cost.ok) errors.push(cost.error);
    if (errors.length) return { ok: false, errors };
    return {
      ok: true,
      payload: { max_tokens: tokens.value, max_cost_usd: cost.value, force_stop: Boolean(f.force_stop) },
    };
  }

  /** "37%" for a ratio (clamped to 0..999). */
  function formatPercent(ratio, digits = 0) {
    const n = safeNum(ratio, 0);
    return `${(Math.min(Math.max(n, 0), 9.99) * 100).toFixed(digits)}%`;
  }

  // -------------------------------------------------------------------------
  // v0.12.1: roster (names, avatars, persona, memory)
  // -------------------------------------------------------------------------

  const NICKNAME_MAX = 20;

  /** Count characters the way the user sees them (an emoji / CJK character is one). */
  function charLength(s) {
    return Array.from(String(s === undefined || s === null ? "" : s)).length;
  }

  /** A nickname: trimmed, 1..20 characters, no control characters, unique among `others` (their names). */
  function validateNickname(raw, others) {
    const name = String(raw === undefined || raw === null ? "" : raw).trim();
    if (!name) return { ok: false, error: "名字不能为空" };
    if (hasControlChars(name)) return { ok: false, error: "名字含有非法控制字符" };
    if (charLength(name) > NICKNAME_MAX) return { ok: false, error: `名字最多 ${NICKNAME_MAX} 个字符` };
    if ((others || []).some((o) => String(o) === name)) return { ok: false, error: `名字「${name}」已被其他玩家使用` };
    return { ok: true, value: name };
  }

  /** GET /api/roster -> {memory_enabled, editable, seats: [{seat, id, name, default_name, avatar, persona, memory}]}. */
  function normalizeRoster(resp) {
    const r = resp && typeof resp === "object" && !Array.isArray(resp) ? resp : {};
    const list = Array.isArray(r.seats) ? r.seats : Array.isArray(resp) ? resp : [];
    const seats = list
      .filter((s) => s && typeof s === "object" && s.id)
      .map((s) => ({
        seat: Number.isFinite(Number(s.seat)) ? Number(s.seat) : 0,
        id: String(s.id),
        name: typeof s.name === "string" ? s.name : String(s.id),
        default_name: typeof s.default_name === "string" ? s.default_name : "",
        avatar: s.avatar && typeof s.avatar === "object" ? s.avatar : null,
        persona: typeof s.persona === "string" ? s.persona : "",
        memory: typeof s.memory === "string" ? s.memory : "",
      }));
    return { memory_enabled: r.memory_enabled !== false, editable: r.editable !== false, seats };
  }

  // -------------------------------------------------------------------------
  // v0.12.1: game clock / sleep labels (agent states)
  // -------------------------------------------------------------------------

  /** 5 -> "5 秒"; 65 -> "1 分 05 秒"; 3700 -> "1 时 01 分". */
  function formatSeconds(seconds) {
    const s = Math.max(0, Math.ceil(safeNum(seconds, 0)));
    if (s < 60) return `${s} 秒`;
    if (s < 3600) return `${Math.floor(s / 60)} 分 ${String(s % 60).padStart(2, "0")} 秒`;
    return `${Math.floor(s / 3600)} 时 ${String(Math.floor((s % 3600) / 60)).padStart(2, "0")} 分`;
  }

  /** "m:ss" countdown text for a request card. */
  function formatCountdown(seconds) {
    const s = Math.max(0, Math.ceil(safeNum(seconds, 0)));
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  }

  /**
   * The game clock "now": the server's clock at the moment we learnt it plus the wall time that has
   * passed since, but only while the game is running (the game clock stops when paused, D18).
   * sync: {clock, at (ms), running}
   */
  function gameClockNow(sync, nowMs) {
    const s = sync || {};
    const base = safeNum(s.clock, 0);
    if (!s.running) return base;
    const elapsed = Math.max(0, (safeNum(nowMs, Date.now()) - safeNum(s.at, 0)) / 1000);
    return base + elapsed;
  }

  /** Tooltip of a sleeping agent: "等待新消息" or "定时醒来：剩余 12 秒". */
  function sleepLabel(sleepUntil, clockNow) {
    if (sleepUntil === null || sleepUntil === undefined || sleepUntil === "" || !Number.isFinite(Number(sleepUntil))) {
      return "等待新消息";
    }
    const left = Number(sleepUntil) - safeNum(clockNow, 0);
    return left > 0 ? `定时醒来：剩余 ${formatSeconds(left)}` : "即将醒来";
  }

  /** Model / depth bindings are locked while a game is running. */
  function bindingsLocked(session) {
    return Boolean(session && session.status === "running");
  }

  function findProvider(providers, id) {
    return (providers || []).find((p) => p.id === id) || null;
  }

  function findModel(provider, modelId) {
    return provider ? (provider.models || []).find((m) => m.id === modelId) || null : null;
  }

  /** Human readable one-liner for a binding. */
  function bindingSummary(binding, providers) {
    if (!binding || !binding.provider_id || !binding.model_id) return "未绑定";
    const provider = findProvider(providers, binding.provider_id);
    const model = findModel(provider, binding.model_id);
    if (!provider || !model) return "⚠ 绑定失效（需重新绑定）";
    return `${provider.name} / ${model.id} · ${binding.reasoning_depth || "high"}`;
  }

  /**
   * rows: [{actor_id, label?, provider_id, model_id, reasoning_depth}]
   * Only rows with a provider+model are sent; a provider without a model is an error.
   */
  function buildBindingsPayload(rows, providers) {
    const errors = [];
    const bindings = {};
    for (const r of rows) {
      const hasP = !isBlank(r.provider_id);
      const hasM = !isBlank(r.model_id);
      if (!hasP && !hasM) continue;
      const label = r.label || r.actor_id;
      if (!hasP || !hasM) {
        errors.push(`${label}：请同时选择 Provider 和模型`);
        continue;
      }
      const provider = findProvider(providers, r.provider_id);
      const model = findModel(provider, r.model_id);
      if (!provider || !model) {
        errors.push(`${label}：所选 Provider/模型不存在`);
        continue;
      }
      const depth = REASONING_DEPTHS.some((d) => d.id === r.reasoning_depth)
        ? r.reasoning_depth
        : "high";
      const b = { provider_id: provider.id, model_id: model.id, reasoning_depth: depth };
      if (typeof model.context_window_tokens === "number") {
        b.context_window_tokens = model.context_window_tokens;
      }
      bindings[r.actor_id] = b;
    }
    if (errors.length) return { ok: false, errors };
    return { ok: true, payload: { bindings } };
  }

  /** Copy the moderator row's provider/model/depth to every participant row. */
  function applyModeratorToAll(rows) {
    const mod = rows.find((r) => r.actor_id === "moderator");
    if (!mod) return rows;
    return rows.map((r) =>
      r.actor_id === "moderator"
        ? r
        : {
            ...r,
            provider_id: mod.provider_id,
            model_id: mod.model_id,
            reasoning_depth: mod.reasoning_depth,
          }
    );
  }

  /** Normalize GET /sessions (array or {sessions:[..]}) to a list of objects. */
  function normalizeSessions(resp) {
    const list = Array.isArray(resp)
      ? resp
      : resp && Array.isArray(resp.sessions)
        ? resp.sessions
        : [];
    return list
      .filter((s) => s && typeof s === "object")
      .map((s) => ({ ...s, session_id: s.session_id || s.id || "" }))
      .filter((s) => s.session_id);
  }

  /** Total cost: the server accumulates per-call price snapshots. */
  function sessionCostUsd(usage) {
    return usage && typeof usage.total_cost_usd === "number" && Number.isFinite(usage.total_cost_usd)
      ? usage.total_cost_usd
      : 0;
  }

  // -------------------------------------------------------------------------
  // v0.11: layout (DOM-free helpers used by layout.js)
  // -------------------------------------------------------------------------

  const LAYOUT_STORAGE_KEY = "arena_layout_v1";

  // v0.12.1: the right-hand "对局状态" column is gone (its numbers live in the participant bar's
  // summary strip), and a "collapsed" channel column shrinks to an avatar rail instead of vanishing.
  const LAYOUT_DEFAULTS = Object.freeze({
    boardCollapsed: false,
    leftCollapsed: false,
    boardHeight: null, // null => natural height (auto)
    leftWidth: 252,
  });

  const PANEL_LIMITS = Object.freeze({
    left: Object.freeze({ min: 190, max: 640 }),
    board: Object.freeze({ min: 96, maxRatio: 0.75 }),
  });

  function clampNumber(value, min, max, fallback) {
    const n = typeof value === "number" ? value : Number(value);
    if (!Number.isFinite(n)) return fallback;
    return Math.min(max, Math.max(min, Math.round(n)));
  }

  function normalizeLayoutState(raw) {
    const src = raw && typeof raw === "object" ? raw : {};
    const leftWidth = clampNumber(
      src.leftWidth,
      PANEL_LIMITS.left.min,
      PANEL_LIMITS.left.max,
      LAYOUT_DEFAULTS.leftWidth
    );
    const rawBoard = Number(src.boardHeight);
    const boardHeight =
      src.boardHeight === null || src.boardHeight === undefined || !Number.isFinite(rawBoard)
        ? null
        : rawBoard >= PANEL_LIMITS.board.min
          ? Math.round(rawBoard)
          : null;
    return {
      boardCollapsed: Boolean(src.boardCollapsed),
      leftCollapsed: Boolean(src.leftCollapsed),
      boardHeight,
      leftWidth,
    };
  }

  function loadLayoutState(storage) {
    try {
      const raw = storage && storage.getItem ? storage.getItem(LAYOUT_STORAGE_KEY) : null;
      if (!raw) return normalizeLayoutState(null);
      return normalizeLayoutState(JSON.parse(raw));
    } catch {
      return normalizeLayoutState(null);
    }
  }

  function saveLayoutState(storage, state) {
    const normalized = normalizeLayoutState(state);
    try {
      if (storage && storage.setItem) {
        storage.setItem(LAYOUT_STORAGE_KEY, JSON.stringify(normalized));
      }
    } catch {
      /* storage unavailable: keep the in-memory state only */
    }
    return normalized;
  }

  // -------------------------------------------------------------------------
  // v0.11: Provider auto_append_path + endpoint preview
  // -------------------------------------------------------------------------

  /** Body for POST /api/providers/endpoint-preview; validates locally first. */
  function buildEndpointPreviewBody(form) {
    const protocol = String((form && form.protocol) || "");
    if (!PROTOCOLS.some((p) => p.id === protocol)) return { ok: false, error: "请选择协议" };
    const base = validateBaseUrl(form && form.base_url);
    if (!base.ok) return { ok: false, error: base.error };
    return {
      ok: true,
      payload: {
        protocol,
        base_url: base.value,
        auto_append_path: !(form && form.auto_append_path === false),
      },
    };
  }

  function normalizeEndpointPreview(resp) {
    const r = resp && typeof resp === "object" ? resp : {};
    const warnings = Array.isArray(r.warnings)
      ? r.warnings.filter((w) => typeof w === "string" && w.trim())
      : [];
    return {
      auto_append_path: r.auto_append_path !== false,
      request_url: typeof r.request_url === "string" ? r.request_url.trim() : "",
      models_url:
        typeof r.models_url === "string" && r.models_url.trim() ? r.models_url.trim() : null,
      warnings,
    };
  }

  /** Model discovery is only possible when the server path is auto-appended. */
  function canDiscoverModels(provider) {
    return providerAutoAppend(provider) && !(provider && provider.read_only);
  }

  // -------------------------------------------------------------------------
  // v0.11: model templates (3 fixed slots)
  // -------------------------------------------------------------------------

  const TEMPLATE_SLOTS = 3;

  /** Normalize GET/PUT /api/settings/templates -> {slots, locked, bySlot}. */
  function normalizeTemplates(resp) {
    const r = resp && typeof resp === "object" && !Array.isArray(resp) ? resp : {};
    const list = Array.isArray(resp) ? resp : Array.isArray(r.templates) ? r.templates : [];
    const declared = Number.isInteger(r.slots) ? r.slots : TEMPLATE_SLOTS;
    const slots = declared >= 1 && declared <= 9 ? declared : TEMPLATE_SLOTS;
    const bySlot = {};
    for (const item of list) {
      if (!item || typeof item !== "object") continue;
      const slot = Number(item.slot);
      if (!Number.isInteger(slot) || slot < 1 || slot > slots || bySlot[slot]) continue;
      const providerId = typeof item.provider_id === "string" ? item.provider_id.trim() : "";
      const modelId = typeof item.model_id === "string" ? item.model_id.trim() : "";
      if (!providerId || !modelId) continue;
      const depth = REASONING_DEPTHS.some((d) => d.id === item.reasoning_depth)
        ? item.reasoning_depth
        : "high";
      bySlot[slot] = {
        slot,
        name:
          typeof item.name === "string" && item.name.trim()
            ? item.name.trim()
            : `模板${slot}`,
        provider_id: providerId,
        model_id: modelId,
        reasoning_depth: depth,
        valid: item.valid !== false,
      };
    }
    return { slots, locked: Boolean(r.locked), bySlot };
  }

  /** Editable rows for every template slot (saved values merged over blanks). */
  function templateRows(normalized) {
    const n =
      normalized && typeof normalized === "object"
        ? normalized
        : normalizeTemplates(null);
    const slots = n.slots || TEMPLATE_SLOTS;
    const rows = [];
    for (let slot = 1; slot <= slots; slot++) {
      const t = n.bySlot ? n.bySlot[slot] : null;
      rows.push({
        slot,
        name: t ? t.name : "",
        provider_id: t ? t.provider_id : "",
        model_id: t ? t.model_id : "",
        reasoning_depth: t ? t.reasoning_depth : "high",
        valid: t ? t.valid !== false : true,
        saved: Boolean(t),
      });
    }
    return rows;
  }

  /** An empty slot is skipped; a partially filled slot is an error (never silent). */
  function buildTemplatesPayload(rows, providers) {
    const errors = [];
    const templates = [];
    for (const row of rows || []) {
      if (!row) continue;
      const slot = Number(row.slot);
      if (!Number.isInteger(slot) || slot < 1 || slot > TEMPLATE_SLOTS) continue;
      const name = String(row.name || "").trim();
      const providerId = String(row.provider_id || "").trim();
      const modelId = String(row.model_id || "").trim();
      if (!name && !providerId && !modelId) continue; // untouched slot: leave it empty
      const label = `模板${slot}`;
      if (!providerId || !modelId) {
        errors.push(`${label}：请同时选择 Provider 和模型`);
        continue;
      }
      if (hasControlChars(name)) {
        errors.push(`${label}：名称含有非法控制字符`);
        continue;
      }
      const provider = findProvider(providers, providerId);
      const model = findModel(provider, modelId);
      if (!provider || !model) {
        errors.push(`${label}：所选 Provider/模型不存在`);
        continue;
      }
      const depth = REASONING_DEPTHS.some((d) => d.id === row.reasoning_depth)
        ? row.reasoning_depth
        : "high";
      templates.push({
        slot,
        name: name || label,
        provider_id: provider.id,
        model_id: model.id,
        reasoning_depth: depth,
      });
    }
    if (errors.length) return { ok: false, errors };
    return { ok: true, payload: { templates } };
  }

  function templateRowComplete(row) {
    return Boolean(
      row && String(row.provider_id || "").trim() && String(row.model_id || "").trim()
    );
  }

  function selectedActorIds(rows) {
    const out = [];
    for (const row of rows || []) {
      if (row && row.selected && row.actor_id && !out.includes(row.actor_id)) {
        out.push(row.actor_id);
      }
    }
    return out;
  }

  /** Display summary for one template (used by apply hints / validity badges). */
  function templateSummary(row, providers) {
    if (!row || !row.provider_id || !row.model_id) return "未填写";
    const provider = findProvider(providers, row.provider_id);
    const model = findModel(provider, row.model_id);
    if (!provider || !model) return "⚠ 引用失效";
    return `${provider.name} / ${model.id} · ${row.reasoning_depth || "high"}`;
  }

  // -------------------------------------------------------------------------
  // v0.11: workspace (工作文件夹)
  // -------------------------------------------------------------------------

  const WORKSPACE_DEFAULT_LABEL = "工作文件夹";

  function workspaceNameFromPath(path) {
    const parts = String(path === undefined || path === null ? "" : path)
      .replace(/[\\/]+$/, "")
      .split(/[\\/]/)
      .filter(Boolean);
    return parts.length ? parts[parts.length - 1] : "";
  }

  /** Normalize one workspace descriptor; returns null when it carries nothing useful. */
  function normalizeWorkspaceInfo(raw) {
    if (!raw || typeof raw !== "object") return null;
    const path = typeof raw.path === "string" ? raw.path.trim() : "";
    let name = typeof raw.name === "string" ? raw.name.trim() : "";
    if (!name) name = workspaceNameFromPath(path);
    const id = raw.id === undefined || raw.id === null ? "" : String(raw.id);
    if (!name && !path && !id) return null;
    return {
      id,
      name: name || WORKSPACE_DEFAULT_LABEL,
      path: path || null,
      last_session:
        raw.last_session === undefined || raw.last_session === null ? null : raw.last_session,
      status: typeof raw.status === "string" ? raw.status : "",
      degraded: Boolean(raw.degraded),
    };
  }

  function normalizeRecentWorkspaces(list) {
    if (!Array.isArray(list)) return [];
    const out = [];
    const seen = new Set();
    for (const item of list) {
      const info = normalizeWorkspaceInfo(item);
      if (!info) continue;
      const key = info.path || info.id || info.name;
      if (seen.has(key)) continue;
      seen.add(key);
      out.push(info);
    }
    return out;
  }

  /** Normalize GET /api/workspaces -> {enabled, current, recent, default_path, native_picker}. */
  function normalizeWorkspaces(resp) {
    const r = resp && typeof resp === "object" && !Array.isArray(resp) ? resp : {};
    return {
      enabled: r.enabled !== false,
      current: normalizeWorkspaceInfo(r.current),
      recent: normalizeRecentWorkspaces(r.recent),
      default_path: typeof r.default_path === "string" && r.default_path.trim()
        ? r.default_path.trim()
        : null,
      native_picker: r.native_picker !== false,
    };
  }

  /** Client-side sanity check; the server still validates the real path rules. */
  function validateWorkspacePath(raw) {
    const s = String(raw === undefined || raw === null ? "" : raw).trim();
    if (!s) return { ok: false, error: "请选择或输入工作文件夹的绝对路径" };
    if (hasControlChars(s)) return { ok: false, error: "路径含有非法控制字符" };
    const winAbs = /^[A-Za-z]:[\\/]/.test(s) || /^\\\\[^\\]/.test(s);
    const posixAbs = /^\//.test(s);
    if (!winAbs && !posixAbs) {
      return { ok: false, error: "必须输入绝对路径（例如 D:\\Arena-Workspace\\MyWorkspace）" };
    }
    if (/^[A-Za-z]:[\\/]?$/.test(s) || s === "/") {
      return { ok: false, error: "不能选择磁盘根目录" };
    }
    return { ok: true, value: s };
  }

  /** Body for POST /api/workspaces/select (optional flags only when ticked). */
  function buildWorkspaceSelectPayload(form) {
    const checked = validateWorkspacePath(form && form.path);
    if (!checked.ok) return checked;
    const payload = { path: checked.value };
    if (form && form.initialize_existing) payload.initialize_existing = true;
    if (form && form.create_if_missing) payload.create_if_missing = true;
    return { ok: true, payload };
  }

  /** Workspace switching must be refused while a game is running (pause first). */
  function workspaceSwitchBlocked(session) {
    return Boolean(session && session.status === "running");
  }

  function workspacePathDisplay(info) {
    if (!info) return "";
    return info.path || "（远程访问不显示完整路径）";
  }

  // -------------------------------------------------------------------------
  // v0.12: number / time formatting (top-bar usage pill, metrics popover, feed)
  // -------------------------------------------------------------------------

  /** 1234 -> "1,234"; 12345 -> "12.3K"; 2_500_000 -> "2.50M" (no unit suffix). */
  function formatCount(value) {
    const n = safeNum(value, 0);
    const abs = Math.abs(n);
    if (abs >= 1_000_000_000) return `${(n / 1_000_000_000).toFixed(2)}B`;
    if (abs >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
    if (abs >= 10_000) return `${(n / 1_000).toFixed(1)}K`;
    return Math.round(n).toLocaleString("en-US");
  }

  /** Top-bar token pill text: "1.5K Tokens" / "2.93M Tokens". */
  function formatTokensCount(value) {
    const n = safeNum(value, 0);
    const abs = Math.abs(n);
    if (abs >= 1_000_000_000) return `${(n / 1_000_000_000).toFixed(2)}B Tokens`;
    if (abs >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M Tokens`;
    if (abs >= 1_000) return `${(n / 1_000).toFixed(1)}K Tokens`;
    return `${Math.round(n).toLocaleString("en-US")} Tokens`;
  }

  /** USD cost with enough digits for tiny per-agent amounts: "$0.0123". */
  function formatCost(value, digits = 4) {
    const n = safeNum(value, 0);
    return `$${Math.max(0, n).toFixed(digits)}`;
  }

  /**
   * Ledger timestamps are wall-clock ISO-8601 strings (display only).
   * Returns "HH:MM:SS" in the viewer's local time, or "" when unparsable.
   */
  function formatClockTime(ts) {
    if (ts === undefined || ts === null || ts === "") return "";
    const d = new Date(ts);
    if (!Number.isNaN(d.getTime())) {
      const p = (x) => String(x).padStart(2, "0");
      return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
    }
    const m = String(ts).match(/(\d{2}:\d{2}:\d{2})/);
    return m ? m[1] : "";
  }

  return {
    PROTOCOLS,
    REASONING_DEPTHS,
    protocolLabel,
    normalizeProviders,
    normalizeModelIds,
    validatePrice,
    validateOptionalPrice,
    validateContextWindow,
    validateBaseUrl,
    buildProviderPayload,
    providerAutoAppend,
    buildEndpointPreviewBody,
    normalizeEndpointPreview,
    canDiscoverModels,
    modelFormMode,
    buildModelPayload,
    bindingsLocked,
    findProvider,
    findModel,
    bindingSummary,
    buildBindingsPayload,
    applyModeratorToAll,
    normalizeSessions,
    sessionCostUsd,
    // v0.11
    LAYOUT_STORAGE_KEY,
    LAYOUT_DEFAULTS,
    PANEL_LIMITS,
    clampNumber,
    normalizeLayoutState,
    loadLayoutState,
    saveLayoutState,
    TEMPLATE_SLOTS,
    normalizeTemplates,
    templateRows,
    buildTemplatesPayload,
    templateRowComplete,
    selectedActorIds,
    templateSummary,
    WORKSPACE_DEFAULT_LABEL,
    workspaceNameFromPath,
    normalizeWorkspaceInfo,
    normalizeWorkspaces,
    validateWorkspacePath,
    buildWorkspaceSelectPayload,
    workspaceSwitchBlocked,
    workspacePathDisplay,
    // v0.12.1
    DEFAULT_PRICING_SOURCE_URL,
    formatDateTime,
    normalizePricingSource,
    validatePricingSourceUrl,
    pricingSourceStatus,
    normalizeSuggestion,
    parseDiscoveredModels,
    suggestionToForm,
    formatPrice,
    BUDGET_WARN_RATIO,
    normalizeBudget,
    budgetStateOf,
    budgetRatio,
    parseBudgetField,
    buildBudgetPayload,
    formatPercent,
    NICKNAME_MAX,
    charLength,
    validateNickname,
    normalizeRoster,
    formatSeconds,
    formatCountdown,
    gameClockNow,
    sleepLabel,
    filenameFromDisposition,
    // v0.12
    formatCount,
    formatTokensCount,
    formatCost,
    formatClockTime,
    escapeHtml,
    safeNum,
    numText,
    stripMarkdownForPreview,
    renderInlineFormatting,
    renderFormattedBlocks,
    renderFormattedText,
    boundedSet,
    prefixFromScriptPath,
    joinUrl,
    buildEventsUrl,
    computeBackoff,
    ApiError,
    formatDetail,
    describeHttpError,
    readBody,
    createApiClient,
  };
});
