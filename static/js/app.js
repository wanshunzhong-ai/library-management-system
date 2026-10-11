/* 图书管理系统 · 前端控制台
 * 无第三方依赖，全部数据经 /api 取得。
 */
(function () {
  "use strict";

  /* ---------------- 基础工具 ---------------- */

  const $ = (sel, root) => (root || document).querySelector(sel);

  const esc = (value) => {
    if (value === null || value === undefined) return "";
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  };

  const debounce = (fn, wait) => {
    let timer = null;
    return function (...args) {
      clearTimeout(timer);
      timer = setTimeout(() => fn.apply(this, args), wait);
    };
  };

  const fmtDateTime = (value) => {
    if (!value) return "—";
    return String(value).slice(0, 16);
  };

  const fmtDate = (value) => {
    if (!value) return "—";
    return String(value).slice(0, 10);
  };

  const money = (value) => "¥" + Number(value || 0).toFixed(2);

  const LIBRARY_NAME = document.body.dataset.library || "图书馆";

  /* ---------------- CSV 导出 ---------------- */

  /** 单元格转义：含逗号 / 引号 / 换行时用双引号包裹，内部引号翻倍。 */
  const csvCell = (value) => {
    const s = value === null || value === undefined ? "" : String(value);
    return /[",\r\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  };

  /** 生成并下载 CSV。开头写入 \uFEFF BOM，Excel 打开中文不乱码。 */
  function downloadCsv(filename, headers, rows) {
    const lines = [headers.map(csvCell).join(",")];
    rows.forEach((row) => lines.push(row.map(csvCell).join(",")));
    const blob = new Blob(["\uFEFF" + lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  const fileStamp = () => {
    const d = new Date();
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}`;
  };

  /** 按当前筛选条件分页拉取全部行（上限 5000 行，避免误点导出拖垮服务）。 */
  async function fetchAllRows(path, params) {
    const pageSize = 100;
    const maxRows = 5000;
    const items = [];
    const maxPage = Math.ceil(maxRows / pageSize);
    for (let page = 1; page <= maxPage; page += 1) {
      const payload = await api.get(path, Object.assign({}, params, { page, page_size: pageSize }));
      const batch = payload.items || [];
      items.push.apply(items, batch);
      if (!batch.length || items.length >= (payload.total || 0) || items.length >= maxRows) break;
    }
    return items.slice(0, maxRows);
  }

  /** 导出期间禁用按钮，避免重复点击；异常统一走 showError。 */
  async function withExport(button, task) {
    if (!button || button.disabled) return;
    const original = button.textContent;
    button.disabled = true;
    button.textContent = "导出中…";
    try {
      await task();
    } catch (err) {
      showError(err);
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  }

  /** 条码号：等宽小标签，便于对标签核对 */
  const barcodeChip = (code) =>
    code
      ? `<span class="barcode-no">${esc(code)}</span>`
      : '<span class="barcode-no is-empty">未设置</span>';

  /* ---------------- Toast ---------------- */

  const toast = (title, body, kind) => {
    const layer = $("#toastLayer");
    const el = document.createElement("div");
    el.className = "toast " + (kind || "ok");
    // 错误用 alert（会打断读屏），普通提示用 status（礼貌播报）
    el.setAttribute("role", kind === "error" ? "alert" : "status");
    el.innerHTML =
      `<button class="t-close" type="button" aria-label="关闭提示">✕</button>` +
      `<div class="t-title">${esc(title)}</div>` +
      (body ? `<div class="t-body">${esc(body)}</div>` : "");
    layer.appendChild(el);

    let timer = null;
    const dismiss = () => {
      clearTimeout(timer);
      el.classList.add("out");
      setTimeout(() => el.remove(), 220);
    };
    el.querySelector(".t-close").addEventListener("click", dismiss);
    const schedule = () => {
      timer = setTimeout(dismiss, kind === "error" ? 6000 : 3600);
    };
    // 悬停暂停自动关闭，方便读完失败原因
    el.addEventListener("mouseenter", () => clearTimeout(timer));
    el.addEventListener("mouseleave", schedule);
    schedule();
  };

  /* ---------------- API ---------------- */

  class ApiError extends Error {
    constructor(message, status, code, field) {
      super(message);
      this.status = status;
      this.code = code;
      this.field = field;
    }
  }

  const auth = { csrf: null, admin: null, redirecting: false, permissions: [], roleLabel: "" };

  /** 当前登录账号是否具备某项能力。权限列表来自 /api/auth/me。 */
  function can(capability) {
    return auth.permissions.indexOf(capability) !== -1;
  }

  function handleUnauthorized() {
    if (auth.redirecting) return;
    auth.redirecting = true;
    toast("登录状态已失效", "即将返回登录页…", "error");
    const next = encodeURIComponent(location.pathname + location.hash);
    setTimeout(() => location.replace("/?next=" + next), 900);
  }

  const api = {
    async request(path, options) {
      const opts = options || {};
      const method = opts.method || "GET";
      const init = { method, headers: { Accept: "application/json" }, credentials: "same-origin" };
      if (opts.body !== undefined) {
        init.headers["Content-Type"] = "application/json";
        init.body = JSON.stringify(opts.body);
      }
      if (method !== "GET" && method !== "HEAD" && auth.csrf) {
        init.headers["X-CSRF-Token"] = auth.csrf;
      }
      let response;
      try {
        response = await fetch(path, init);
      } catch (err) {
        throw new ApiError("无法连接服务器，请确认服务是否在运行", 0, "network_error");
      }
      // 拿到响应的请求都会让服务端滑动续期（401 除外，那时会话已经没了），同步刷新闲置计时
      if (response.status !== 401) sessionWatch.touch();
      const text = await response.text();
      let payload = null;
      if (text) {
        try { payload = JSON.parse(text); } catch (err) { payload = null; }
      }
      if (!response.ok) {
        const errObj = (payload && payload.error) || {};
        if (response.status === 401) handleUnauthorized();
        let message = errObj.message || `请求失败（HTTP ${response.status}）`;
        // 5xx 追加后端 request_id 短码：客服/排查时可直接对上服务端日志
        const rid = payload && payload.request_id;
        if (response.status >= 500 && rid) {
          message += `（错误编号 ${String(rid).slice(0, 8)}）`;
        }
        throw new ApiError(
          message,
          response.status,
          errObj.code || "http_error",
          errObj.details && errObj.details.field
        );
      }
      return payload;
    },
    get(path, params) {
      const qs = params ? "?" + new URLSearchParams(params).toString() : "";
      return api.request(path + qs);
    },
    post(path, body) { return api.request(path, { method: "POST", body: body || {} }); },
    put(path, body) { return api.request(path, { method: "PUT", body: body || {} }); },
    del(path) { return api.request(path, { method: "DELETE" }); },
  };

  /* ---------------- 会话到期提醒 ---------------- */

  // 后端会话是滑动续期（任意请求都会重置 Cookie 有效期），所以「闲置时长」以最后一次
  // 拿到响应的请求为基准：闲置到「有效期 - 预警窗口」就提示，闲置超过有效期则判定失效。
  // 只认真实请求、不认页面上的输入动作 —— 表单里打字并不会续期，这正是需要提醒的场景。
  const SESSION_WARN_BEFORE = 120; // 到期前 120 秒开始提醒
  const SESSION_TICK_MS = 15000;

  const sessionWatch = {
    lifetime: 0,
    lastActivityAt: Date.now(),
    warned: false,
    expired: false,
    timer: null,

    /** 任意一次拿到服务端响应的请求都算活跃：重置闲置计时并收起横幅。 */
    touch() {
      this.lastActivityAt = Date.now();
      if (!this.timer) return;
      this.warned = false;
      this.expired = false;
      hideSessionBanner();
    },

    start(lifetimeSeconds) {
      this.stop();
      const seconds = Number(lifetimeSeconds) || 0;
      // 会话本身就比预警窗口还短时，提前提醒没有意义
      if (seconds <= SESSION_WARN_BEFORE * 2) return;
      this.lifetime = seconds;
      this.lastActivityAt = Date.now();
      this.warned = false;
      this.expired = false;
      this.timer = setInterval(() => this.tick(), SESSION_TICK_MS);
    },

    stop() {
      if (this.timer) clearInterval(this.timer);
      this.timer = null;
    },

    idleSeconds() {
      return (Date.now() - this.lastActivityAt) / 1000;
    },

    tick() {
      if (!this.timer) return;
      const idle = this.idleSeconds();
      if (idle >= this.lifetime) {
        if (!this.expired) {
          this.expired = true;
          this.warned = true;
          showSessionExpired();
        }
        return;
      }
      if (idle >= this.lifetime - SESSION_WARN_BEFORE && !this.warned) {
        this.warned = true;
        showSessionWarning(Math.max(1, Math.ceil((this.lifetime - idle) / 60)));
      }
    },
  };

  function hideSessionBanner() {
    const banner = $("#sessionBanner");
    if (banner) banner.hidden = true;
  }

  function showSessionWarning(minutes) {
    const banner = $("#sessionBanner");
    if (!banner) return;
    $("#sessionBannerTitle").textContent = "会话即将过期";
    $("#sessionBannerText").textContent =
      `已闲置较长时间，约 ${minutes} 分钟后需要重新登录，弹窗中未保存的内容会丢失。`;
    const btn = $("#sessionBannerBtn");
    btn.textContent = "保持登录";
    btn.dataset.action = "keepalive";
    banner.hidden = false;
  }

  function showSessionExpired() {
    const banner = $("#sessionBanner");
    if (!banner) return;
    $("#sessionBannerTitle").textContent = "会话已过期";
    $("#sessionBannerText").textContent = "登录状态已失效，请重新登录后继续，弹窗中未保存的内容会丢失。";
    const btn = $("#sessionBannerBtn");
    btn.textContent = "重新登录";
    btn.dataset.action = "relogin";
    banner.hidden = false;
  }

  function onSessionBannerClick() {
    const btn = $("#sessionBannerBtn");
    if (btn.dataset.action === "relogin") {
      const next = encodeURIComponent(location.pathname + location.hash);
      location.replace("/?next=" + next);
      return;
    }
    // 保持登录：发一个轻量请求把服务端会话续上；成功后 touch() 会自动收起横幅
    api.get("/api/auth/me").catch(() => {});
  }

  /* ---------------- 全局兜底 ---------------- */

  // 未捕获的 Promise 拒绝 / 运行时报错统一转成 Toast，避免出现「点了没反应」的静默失败。
  // 5 秒内相同文案去重，防止一个坏掉的轮询把界面刷满。
  let lastGlobalError = { message: "", at: 0 };

  function reportGlobalError(message) {
    const now = Date.now();
    if (message === lastGlobalError.message && now - lastGlobalError.at < 5000) return;
    lastGlobalError = { message, at: now };
    toast("页面出现异常", message || "未知错误，请刷新页面后重试", "error");
  }

  window.addEventListener("unhandledrejection", (event) => {
    const reason = event.reason;
    // ApiError 一般在调用处已经提示过（showError / showModalError），这里不重复报
    if (reason instanceof ApiError) return;
    reportGlobalError(reason && reason.message ? reason.message : String(reason || ""));
  });

  window.addEventListener("error", (event) => {
    // 资源加载失败（img/script）没有 message/error，忽略以免误报
    if (!event.message && !event.error) return;
    reportGlobalError(event.message || (event.error && event.error.message) || "");
  });

  /* ---------------- 视图元数据 ---------------- */

  // permission 字段存在的视图表示「需要该权限才看得到」：缺权限时导航入口隐藏、
  // 手改 hash 也会被挡回概览。后端对这些接口独立校验，前端只是不给出入口。
  const VIEWS = {
    dashboard: { title: "概览", sub: "馆藏与流通实时概况" },
    books: { title: "图书管理", sub: "维护馆藏书目与库存" },
    members: { title: "读者管理", sub: "维护读者档案与借阅权限" },
    loans: { title: "借阅管理", sub: "办理借书、还书与续借" },
    accounts: { title: "账号管理", sub: "后台账号、角色与启停用", permission: "account:manage" },
    logs: { title: "操作日志", sub: "谁在什么时候改了什么", permission: "log:read" },
  };

  const state = {
    view: "dashboard",
    books: { page: 1, pageSize: 10, keyword: "", category: "", availableOnly: false, sort: "", order: "" },
    members: { page: 1, pageSize: 10, keyword: "", status: "", sort: "", order: "" },
    loans: { page: 1, pageSize: 10, keyword: "", status: "all", sort: "", order: "" },
    accounts: { keyword: "" },
    logs: { page: 1, pageSize: 20, keyword: "", actorType: "", action: "", status: "" },
    categories: [],
  };

  /* ---------------- 加载态 ---------------- */

  // 各列表的表头，供首次加载时渲染骨架行（列数必须与真实表格一致）
  const LOADING = {
    books: {
      table: "#bookTable",
      headers: ["书名 / 作者 / 简介", "条码 / ISBN", "分类", "出版社", "年份", "位置", "库存", "操作"],
    },
    members: {
      table: "#memberTable",
      headers: ["姓名 / 证号", "类型", "联系方式", "可借上限", "在借", "逾期", "状态", "门户账号", "操作"],
    },
    loans: {
      table: "#loanTable",
      headers: ["选择", "图书", "读者", "借出日期", "应还日期", "归还日期", "续借", "状态", "罚金", "操作"],
    },
    accounts: {
      table: "#accountTable",
      headers: ["用户名", "姓名", "角色", "状态", "最近登录", "创建时间", "操作"],
    },
    logs: {
      table: "#logTable",
      headers: ["时间", "操作者", "动作", "对象", "结果", "摘要"],
    },
  };

  /** 首次进入某视图时先铺骨架行；之后的数据刷新只做整卡降噪，避免内容跳变。 */
  function beginLoad(view) {
    const cfg = LOADING[view];
    const table = cfg && $(cfg.table);
    if (!table) return;
    if (table.dataset.loaded !== "1") {
      const head = cfg.headers.map((h) => `<th>${esc(h)}</th>`).join("");
      const body = Array.from({ length: 5 })
        .map(
          () =>
            `<tr>${cfg.headers
              .map((_, i) => `<td><span class="skeleton ${i % 3 === 1 ? "narrow" : i % 3 === 0 ? "wide" : ""}"></span></td>`)
              .join("")}</tr>`
        )
        .join("");
      table.innerHTML = `<thead><tr>${head}</tr></thead><tbody>${body}</tbody>`;
    }
    const card = table.closest(".card");
    if (card) {
      card.classList.add("is-loading");
      card.setAttribute("aria-busy", "true");
    }
  }

  function endLoad(view) {
    const cfg = LOADING[view];
    const table = cfg && $(cfg.table);
    if (!table) return;
    table.dataset.loaded = "1";
    const card = table.closest(".card");
    if (card) {
      card.classList.remove("is-loading");
      card.removeAttribute("aria-busy");
    }
  }

  /** 首次加载失败时把骨架换成可重试的错误态（已有数据时不覆盖旧内容）。 */
  function loadFailed(view) {
    const cfg = LOADING[view];
    const table = cfg && $(cfg.table);
    if (!table || table.dataset.loaded === "1") return;
    table.innerHTML = `<tbody><tr><td class="empty" colspan="${cfg.headers.length}">
      <div>数据加载失败，请检查服务是否在运行</div>
      <button class="btn sm" type="button" data-reload>重试</button>
    </td></tr></tbody>`;
    const btn = table.querySelector("[data-reload]");
    if (btn) btn.addEventListener("click", () => refreshCurrentView().catch(showError));
  }

  /** 空列表统一给出「清除筛选」出口（三个视图共用）。 */
  function emptyState(colspan, hint, clearLabel) {
    return `<tbody><tr><td class="empty" colspan="${colspan}">
      <div>${esc(hint)}</div>
      ${clearLabel ? `<button class="btn sm" type="button" data-clear>${esc(clearLabel)}</button>` : ""}
    </td></tr></tbody>`;
  }

  /* ---------------- 排序表头 ---------------- */

  // 三个列表可排序的列（必须在后端的白名单里）。点击表头在
  // 「升序 → 降序 → 默认」之间循环，状态同时写进 URL，刷新后仍然生效。
  const SORT_KEYS = {
    books: { title: 1, category: 1, publisher: 1, publish_year: 1, total_copies: 1, available_copies: 1 },
    members: { name: 1, member_type: 1, max_borrow: 1, active_loans: 1, overdue_loans: 1, status: 1 },
    loans: {
      book_title: 1, member_name: 1, borrowed_at: 1, due_at: 1,
      returned_at: 1, renew_count: 1, status: 1, fine: 1,
    },
  };

  /** 可排序表头：带排序指示与 aria-sort，供读屏与视觉同步。 */
  function sortHeader(view, key, label, cls) {
    const extra = cls ? ` ${cls}` : "";
    if (!SORT_KEYS[view][key]) return `<th class="${cls || ""}">${esc(label)}</th>`;
    const s = state[view];
    const active = s.sort === key;
    const dir = active ? s.order : "";
    const mark = active ? (dir === "asc" ? "▲" : "▼") : "";
    return `<th class="sortable${extra}" data-sort="${esc(key)}" aria-sort="${
      active ? (dir === "asc" ? "ascending" : "descending") : "none"
    }" title="点击排序：升序 → 降序 → 取消">
      <span class="th-label">${esc(label)}</span><span class="sort-ind" aria-hidden="true">${mark}</span>
    </th>`;
  }

  /** 点击表头：切换该列排序（升 → 降 → 取消），并回到第 1 页。 */
  function toggleSort(view, key) {
    const s = state[view];
    if (s.sort !== key) {
      s.sort = key;
      s.order = "asc";
    } else if (s.order === "asc") {
      s.order = "desc";
    } else {
      s.sort = "";
      s.order = "";
    }
    s.page = 1;
    syncUrl();
    refreshCurrentView().catch(showError);
  }

  /* ---------------- URL 路由与筛选持久化 ---------------- */

  // 视图与筛选条件全部写进 URL（#view?kw=…&page=2）。这样刷新 / 前进后退 / 分享链接
  // 都能还原到同一个列表状态；浏览器前进后退也能真正切换视图（原先只有首次加载读 hash）。
  const DEFAULT_PAGE_SIZE = 10;

  function buildHash(view) {
    const params = new URLSearchParams();
    const write = (s) => {
      if (s.keyword) params.set("kw", s.keyword);
      if (s.page > 1) params.set("page", String(s.page));
      if (s.pageSize !== DEFAULT_PAGE_SIZE) params.set("size", String(s.pageSize));
      if (s.sort) {
        params.set("sort", s.sort);
        params.set("order", s.order === "desc" ? "desc" : "asc");
      }
    };
    if (view === "books") {
      write(state.books);
      if (state.books.category) params.set("cat", state.books.category);
      if (state.books.availableOnly) params.set("avail", "1");
    } else if (view === "members") {
      write(state.members);
      if (state.members.status) params.set("st", state.members.status);
    } else if (view === "loans") {
      write(state.loans);
      if (state.loans.status && state.loans.status !== "all") params.set("st", state.loans.status);
    } else if (view === "accounts") {
      if (state.accounts.keyword) params.set("kw", state.accounts.keyword);
    } else if (view === "logs") {
      const s = state.logs;
      if (s.keyword) params.set("kw", s.keyword);
      if (s.page > 1) params.set("page", String(s.page));
      if (s.pageSize !== 20) params.set("size", String(s.pageSize));
      if (s.actorType) params.set("actor", s.actorType);
      if (s.action) params.set("action", s.action);
      if (s.status) params.set("st", s.status);
    }
    const qs = params.toString();
    return "#" + view + (qs ? "?" + qs : "");
  }

  function readHash(hash) {
    const raw = (hash || "").replace(/^#/, "");
    const questionAt = raw.indexOf("?");
    const view = questionAt === -1 ? raw : raw.slice(0, questionAt);
    return { view, params: new URLSearchParams(questionAt === -1 ? "" : raw.slice(questionAt + 1)) };
  }

  const positiveInt = (value, fallback) => {
    const n = Number(value);
    return Number.isFinite(n) && n > 0 ? Math.floor(n) : fallback;
  };

  /** 把当前 state 回填到工具栏控件，保证「控件 / state / URL」三者始终一致。 */
  function syncToolbar(view) {
    const v = view || state.view;
    if (v === "books") {
      $("#bookKeyword").value = state.books.keyword;
      $("#bookCategory").value = state.books.category;
      $("#bookAvailableOnly").checked = state.books.availableOnly;
    } else if (v === "members") {
      $("#memberKeyword").value = state.members.keyword;
      $("#memberStatus").value = state.members.status;
    } else if (v === "loans") {
      $("#loanKeyword").value = state.loans.keyword;
      document.querySelectorAll("#loanTabs .tab").forEach((t) =>
        t.classList.toggle("is-active", t.dataset.status === state.loans.status)
      );
    } else if (v === "accounts") {
      const box = $("#accountKeyword");
      if (box) box.value = state.accounts.keyword;
    } else if (v === "logs") {
      const kw = $("#logKeyword");
      if (kw) kw.value = state.logs.keyword;
      const actor = $("#logActorType");
      if (actor) actor.value = state.logs.actorType;
      const action = $("#logAction");
      if (action) action.value = state.logs.action;
      const status = $("#logStatus");
      if (status) status.value = state.logs.status;
    }
  }

  /** 把 URL 参数回填到 state（供初始化与前进后退使用）。 */
  function applyHashParams(view, params) {
    const kw = params.get("kw") || "";
    const page = positiveInt(params.get("page"), 1);
    const size = positiveInt(params.get("size"), DEFAULT_PAGE_SIZE);
    const sort = params.get("sort") || "";
    const order = sort ? (params.get("order") === "desc" ? "desc" : "asc") : "";
    if (view === "books") {
      Object.assign(state.books, {
        keyword: kw,
        category: params.get("cat") || "",
        availableOnly: params.get("avail") === "1",
        page,
        pageSize: size,
        sort,
        order,
      });
    } else if (view === "members") {
      Object.assign(state.members, {
        keyword: kw,
        status: params.get("st") || "",
        page,
        pageSize: size,
        sort,
        order,
      });
    } else if (view === "loans") {
      Object.assign(state.loans, {
        keyword: kw,
        status: params.get("st") || "all",
        page,
        pageSize: size,
        sort,
        order,
      });
    } else if (view === "accounts") {
      Object.assign(state.accounts, { keyword: kw });
    } else if (view === "logs") {
      Object.assign(state.logs, {
        keyword: kw,
        page,
        pageSize: positiveInt(params.get("size"), 20),
        actorType: params.get("actor") || "",
        action: params.get("action") || "",
        status: params.get("st") || "",
      });
    }
    syncToolbar(view);
  }

  /** 把当前视图与筛选写回地址栏（replaceState 不产生历史条目，也不触发 hashchange）。 */
  function syncUrl() {
    const next = buildHash(state.view);
    if (location.hash !== next) {
      history.replaceState(null, "", location.pathname + location.search + next);
    }
  }

  /** 切到指定筛选 / 页码：更新 state + 回填控件 + 写回 URL + 重新拉取。 */
  function updateFilters(patch) {
    const target = state[state.view];
    if (target) Object.assign(target, patch);
    syncToolbar();
    syncUrl();
    refreshCurrentView().catch(showError);
  }

  /* ---------------- 渲染：概览 ---------------- */

  function statCard(label, value, foot, tone) {
    return `<div class="stat ${tone || ""}">
      <div class="label">${esc(label)}</div>
      <div class="value">${esc(value)}</div>
      <div class="foot">${esc(foot || "")}</div>
    </div>`;
  }

  /** 侧栏借阅策略：概览与登录会话都会带 policy，任意视图刷新后都能显示 */
  function renderPolicy(policy) {
    if (!policy) return; // 后端未提供时保留占位符，不渲染成 0
    const p = policy;
    $("#policyDays").textContent = (p.loan_days || 0) + " 天";
    $("#policyRenew").textContent = (p.max_renew || 0) + " 次";
    $("#policyFine").textContent = money(p.fine_per_day) + "/天";
    const fineCap = Number(p.fine_cap) || 0;
    const fineMaxDays = Number(p.fine_max_days) || 0;
    $("#policyFineCap").textContent = fineCap > 0
      ? `${money(fineCap)} / 笔${fineMaxDays > 0 ? `（${fineMaxDays} 天）` : ""}`
      : fineMaxDays > 0
        ? `按 ${fineMaxDays} 天封顶`
        : "不封顶";
  }

  function renderDashboard(data) {
    const b = data.books || {};
    const m = data.members || {};
    const l = data.loans || {};

    const offShelf = Number(b.off_shelf_titles) || 0;
    $("#statGrid").innerHTML = [
      statCard(
        "馆藏书目",
        b.total_titles || 0,
        `共 ${b.total_copies || 0} 册${offShelf ? ` · 已下架 ${offShelf} 种` : ""}`,
        ""
      ),
      statCard("可借册数", b.available_copies || 0, `${b.borrowed_copies || 0} 册在外`, "is-ok"),
      statCard("在借未还", l.open_loans || 0, `今日借出 ${l.borrowed_today || 0} 册`, ""),
      statCard("逾期未还", l.overdue_loans || 0, `预估罚金 ${money(data.fine_estimate)}`, (l.overdue_loans || 0) > 0 ? "is-danger" : "is-ok"),
      statCard("7 天内到期", l.due_soon_loans || 0, "建议提前提醒", (l.due_soon_loans || 0) > 0 ? "is-warn" : ""),
      statCard("注册读者", m.total_members || 0, `正常 ${m.active_members || 0} · 冻结 ${m.frozen_members || 0}`, ""),
    ].join("");

    renderPolicy(data.policy);

    $("#trendHint").textContent = `近 ${(data.trend || []).length} 天`;
    $("#trendChart").innerHTML = renderTrend(data.trend || []);
    $("#categoryBars").innerHTML = renderBars(data.category_ranking || []);
    $("#hotBooks").innerHTML = renderRanks(data.hot_books || []);
    renderOverdueTable(data.overdue_loans || []);
  }

  function renderTrend(series) {
    if (!series.length) return `<div class="chart-empty">暂无借阅数据</div>`;

    const W = 640, H = 210;
    const pad = { top: 16, right: 14, bottom: 28, left: 34 };
    const innerW = W - pad.left - pad.right;
    const innerH = H - pad.top - pad.bottom;

    const counts = series.map((d) => Number(d.count) || 0);
    const rawMax = Math.max.apply(null, counts.concat([1]));
    const step = rawMax <= 5 ? 1 : Math.ceil(rawMax / 4);
    const maxY = step * 4;

    const x = (i) => pad.left + (series.length === 1 ? innerW / 2 : (innerW * i) / (series.length - 1));
    const y = (v) => pad.top + innerH - (innerH * v) / maxY;

    const linePts = series.map((d, i) => `${x(i).toFixed(1)},${y(Number(d.count) || 0).toFixed(1)}`);
    const areaPath =
      `M ${x(0).toFixed(1)},${(pad.top + innerH).toFixed(1)} L ` +
      linePts.join(" L ") +
      ` L ${x(series.length - 1).toFixed(1)},${(pad.top + innerH).toFixed(1)} Z`;

    let grid = "";
    for (let i = 0; i <= 4; i++) {
      const gy = pad.top + (innerH * i) / 4;
      const val = Math.round(maxY - (maxY * i) / 4);
      grid += `<line x1="${pad.left}" y1="${gy.toFixed(1)}" x2="${W - pad.right}" y2="${gy.toFixed(1)}" stroke="#eef1f6" stroke-width="1" />`;
      grid += `<text x="${pad.left - 8}" y="${(gy + 3.5).toFixed(1)}" text-anchor="end" font-size="10" fill="#8b95a7">${val}</text>`;
    }

    const labelStep = Math.max(1, Math.ceil(series.length / 8));
    let labels = "";
    let dots = "";
    series.forEach((d, i) => {
      if (i % labelStep === 0 || i === series.length - 1) {
        labels += `<text x="${x(i).toFixed(1)}" y="${H - 8}" text-anchor="middle" font-size="10" fill="#8b95a7">${esc(d.label || "")}</text>`;
      }
      const cv = Number(d.count) || 0;
      if (cv > 0) {
        dots += `<circle cx="${x(i).toFixed(1)}" cy="${y(cv).toFixed(1)}" r="2.6" fill="#2f6feb" stroke="#fff" stroke-width="1.4"><title>${esc(d.day)}：${cv} 册</title></circle>`;
      }
    });

    return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="借阅趋势">
      <defs>
        <linearGradient id="areaFill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="#2f6feb" stop-opacity="0.22" />
          <stop offset="100%" stop-color="#2f6feb" stop-opacity="0" />
        </linearGradient>
      </defs>
      ${grid}
      <path d="${areaPath}" fill="url(#areaFill)" />
      <polyline points="${linePts.join(" ")}" fill="none" stroke="#2f6feb" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" />
      ${dots}
      ${labels}
    </svg>`;
  }

  function renderBars(rows) {
    if (!rows.length) return `<div class="empty">暂无数据</div>`;
    const max = Math.max.apply(null, rows.map((r) => Number(r.loan_count) || 0).concat([1]));
    return rows
      .map((r) => {
        const value = Number(r.loan_count) || 0;
        const pct = Math.max(3, Math.round((value / max) * 100));
        return `<div class="bar-row">
          <span title="${esc(r.name)}">${esc(r.name)}</span>
          <span class="bar-track"><span class="bar-fill" style="width:${pct}%"></span></span>
          <span class="bar-value">${value}</span>
        </div>`;
      })
      .join("");
  }

  function renderRanks(rows) {
    if (!rows.length) return `<div class="empty">暂无数据</div>`;
    return rows
      .map(
        (r, i) => `<div class="rank-item">
          <span class="rank-no">${i + 1}</span>
          <span class="rank-main">
            <span class="name" title="${esc(r.title)}">${esc(r.title)}</span>
            <span class="meta">${esc(r.author || "佚名")}</span>
          </span>
          <span class="rank-count">${Number(r.loan_count) || 0} 次</span>
        </div>`
      )
      .join("");
  }

  function renderOverdueTable(rows) {
    const table = $("#overdueTable");
    if (!rows.length) {
      table.innerHTML = `<tbody><tr><td class="empty">当前没有逾期图书，状态良好 🎉</td></tr></tbody>`;
      return;
    }
    table.innerHTML = `
      <thead><tr><th>图书</th><th>读者</th><th>应还日期</th><th>逾期</th><th>罚金</th></tr></thead>
      <tbody>${rows
        .map(
          (r) => `<tr>
            <td class="wrap title-cell"><strong>${esc(r.book_title)}</strong></td>
            <td>${esc(r.member_name)} <span class="sub">${esc(r.member_card)}</span></td>
            <td class="sub">${esc(fmtDate(r.due_at))}</td>
            <td><span class="badge danger">${Number(r.overdue_days) || 0} 天</span></td>
            <td class="num">${money(r.fine)}</td>
          </tr>`
        )
        .join("")}</tbody>`;
  }

  /* ---------------- 分页组件 ---------------- */

  function renderPager(node, payload, onChange) {
    const { page, pages, total, page_size: size } = payload;
    node.innerHTML = `
      <span class="page-info">共 ${total} 条记录 · 第 ${page}/${pages} 页</span>
      <select class="select" data-size>
        ${[10, 20, 50].map((n) => `<option value="${n}" ${n === size ? "selected" : ""}>${n} 条/页</option>`).join("")}
      </select>
      <button class="btn sm" data-go="1" ${page <= 1 ? "disabled" : ""}>« 首页</button>
      <button class="btn sm" data-go="${page - 1}" ${page <= 1 ? "disabled" : ""}>上一页</button>
      <button class="btn sm" data-go="${page + 1}" ${page >= pages ? "disabled" : ""}>下一页</button>
      <button class="btn sm" data-go="${pages}" ${page >= pages ? "disabled" : ""}>末页 »</button>`;

    node.querySelectorAll("[data-go]").forEach((btn) => {
      btn.addEventListener("click", () => onChange({ page: Number(btn.dataset.go) }));
    });
    const sizeSel = node.querySelector("[data-size]");
    if (sizeSel) sizeSel.addEventListener("change", () => onChange({ page: 1, pageSize: Number(sizeSel.value) }));
  }

  /* ---------------- 图书视图 ---------------- */

  /** 统一包裹列表加载：骨架 / 降噪 / 失败重试，调用方只需关心渲染。 */
  async function withViewLoading(view, task) {
    beginLoad(view);
    try {
      await task();
    } catch (err) {
      loadFailed(view);
      throw err;
    } finally {
      endLoad(view);
    }
  }

  async function loadBooks() {
    const s = state.books;
    await withViewLoading("books", async () => {
      const payload = await api.get("/api/books", {
        keyword: s.keyword,
        category: s.category,
        available_only: s.availableOnly ? "1" : "",
        sort: s.sort,
        order: s.order,
        page: s.page,
        page_size: s.pageSize,
      });
      const table = $("#bookTable");
      if (!payload.items.length) {
        const hasFilter = Boolean(s.keyword || s.category || s.availableOnly);
        table.innerHTML = emptyState(8, "没有匹配的图书", hasFilter ? "清除筛选条件" : "");
        const clear = table.querySelector("[data-clear]");
        if (clear) clear.addEventListener("click", resetBooks);
      } else {
        table.innerHTML = `
          <thead><tr>
            ${sortHeader("books", "title", "书名 / 作者 / 简介")}<th>条码 / ISBN</th>${sortHeader("books", "category", "分类")}${sortHeader("books", "publisher", "出版社")}${sortHeader("books", "publish_year", "年份")}<th>位置</th>${sortHeader("books", "available_copies", "库存")}<th>操作</th>
          </tr></thead>
          <tbody>${payload.items
            .map((b) => {
              const avail = Number(b.available_copies) || 0;
              const total = Number(b.total_copies) || 0;
              const offShelf = !!b.off_shelf || total === 0;
              const tone = avail === 0 ? "danger" : avail <= 1 ? "warn" : "ok";
              const stock = offShelf
                ? `<span class="badge neutral">已下架</span>`
                : `<span class="badge ${tone}">${avail} / ${total}</span>`;
              return `<tr${offShelf ? ' class="is-off-shelf"' : ""}>
                <td class="wrap title-cell">
                  <strong>${esc(b.title)}</strong>
                  <div class="sub">${esc(b.author || "佚名")}</div>
                  <div class="book-desc">${b.description ? esc(b.description) : "暂无简介"}</div>
                </td>
                <td class="barcode-cell">${barcodeChip(b.barcode)}<div class="sub">${esc(b.isbn)}</div></td>
                <td><span class="badge neutral">${esc(b.category)}</span></td>
                <td>${esc(b.publisher || "—")}</td>
                <td class="num sub">${b.publish_year ? esc(b.publish_year) : "—"}</td>
                <td class="sub">${esc(b.location || "—")}</td>
                <td>${stock}</td>
                <td><div class="actions">
                  <button class="btn sm" data-detail="${b.id}">详情</button>
                  ${can("book:write") ? `<button class="btn sm" data-edit="${b.id}">编辑</button>
                  <button class="btn sm" data-label="${b.id}" title="打印馆藏条码标签">标签</button>
                  <button class="btn sm" data-shelf="${b.id}" data-off="${offShelf ? "0" : "1"}" data-title="${esc(b.title)}" title="${offShelf ? "恢复到架上，读者可检索与借阅" : "从读者门户移除，不可借出（需先还清在借册数）"}">${offShelf ? "上架" : "下架"}</button>
                  <button class="btn sm danger" data-del="${b.id}" data-title="${esc(b.title)}">删除</button>` : ""}
                  ${can("loan:write") ? `<button class="btn sm" data-borrow="${b.id}" ${offShelf || avail === 0 ? "disabled" : ""} title="${offShelf ? "已下架，无法借出" : avail === 0 ? "当前无可借库存" : "为该图书办理借书"}">借出</button>` : ""}
                </div></td>
              </tr>`;
            })
            .join("")}</tbody>`;
      }

      renderPager($("#bookPager"), payload, (patch) => updateFilters(patch));

      table.querySelectorAll("[data-detail]").forEach((btn) =>
        btn.addEventListener("click", () => openBookDetail(Number(btn.dataset.detail)))
      );
      table.querySelectorAll("[data-edit]").forEach((btn) =>
        btn.addEventListener("click", () => openBookForm(Number(btn.dataset.edit)))
      );
      table.querySelectorAll("[data-label]").forEach((btn) =>
        btn.addEventListener("click", () => openLabelDialog(Number(btn.dataset.label)))
      );
      table.querySelectorAll("[data-del]").forEach((btn) =>
        btn.addEventListener("click", () => confirmDeleteBook(Number(btn.dataset.del), btn.dataset.title))
      );
      table.querySelectorAll("[data-shelf]").forEach((btn) =>
        btn.addEventListener("click", () =>
          toggleShelf(Number(btn.dataset.shelf), btn.dataset.off === "1", btn.dataset.title)
        )
      );
      table.querySelectorAll("[data-borrow]").forEach((btn) =>
        btn.addEventListener("click", () => openBorrowForm(Number(btn.dataset.borrow)))
      );
    });
  }

  function resetBooks() {
    state.books = {
      page: 1, pageSize: state.books.pageSize, keyword: "", category: "",
      availableOnly: false, sort: "", order: "",
    };
    $("#bookKeyword").value = "";
    $("#bookCategory").value = "";
    $("#bookAvailableOnly").checked = false;
    syncUrl();
    loadBooks().catch(showError);
  }

  async function loadCategories() {
    const payload = await api.get("/api/books/categories");
    state.categories = payload.items || [];
    const sel = $("#bookCategory");
    const current = state.books.category;
    sel.innerHTML =
      `<option value="">全部分类</option>` +
      state.categories
        .map((c) => `<option value="${esc(c.name)}" ${c.name === current ? "selected" : ""}>${esc(c.name)}（${c.book_count}）</option>`)
        .join("");
  }

  function openBookForm(id) {
    const isEdit = !!id;
    const load = isEdit ? api.get(`/api/books/${id}`) : Promise.resolve(null);
    load
      .then((book) => {
        const b = book || {};
        // ISBN 校验位只做提示，不阻断保存（历史数据里可能有内部编码）
        const isbnTip =
          b.isbn_check === "invalid_checksum"
            ? `<span class="tip is-warn">当前 ISBN 校验位不正确，若非历史数据请核对</span>`
            : `<span class="tip">可含连字符，保存时自动规整</span>`;
        openModal({
          title: isEdit ? `编辑图书 · 《${b.title || id}》` : "新增图书",
          body: `
            <div class="form-grid">
              <div class="field full"><label>书名<span class="req">*</span></label><input name="title" value="${esc(b.title || "")}" placeholder="如：Python 编程：从入门到实践" /></div>
              <div class="field"><label>ISBN<span class="req">*</span></label><input name="isbn" value="${esc(b.isbn || "")}" placeholder="10 位或 13 位" />${isbnTip}</div>
              <div class="field"><label>馆藏条码</label><input name="barcode" value="${esc(b.barcode || "")}" placeholder="留空则自动生成 BK 编号" /><span class="tip">4–32 位字母、数字或连字符；留空自动分配，也可填馆内已有条码</span></div>
              <div class="field"><label>作者</label><input name="author" value="${esc(b.author || "")}" /></div>
              <div class="field"><label>出版社</label><input name="publisher" value="${esc(b.publisher || "")}" /></div>
              <div class="field"><label>分类</label><input name="category" value="${esc(b.category || "")}" placeholder="未分类" /></div>
              <div class="field"><label>出版年份</label><input name="publish_year" value="${b.publish_year ? esc(b.publish_year) : ""}" placeholder="2024" /></div>
              <div class="field"><label>馆藏位置</label><input name="location" value="${esc(b.location || "")}" placeholder="A-01-03" /></div>
              <div class="field"><label>总册数<span class="req">*</span></label><input name="total_copies" value="${b.total_copies !== undefined ? esc(b.total_copies) : 1}" />
                <span class="tip">${isEdit ? `在借 ${b.active_loans || 0} 册，总册数不可小于该值；填 0 表示下架（不再对读者展示）` : "可借数将自动等于总册数"}</span>
              </div>
              <div class="field full"><label>图书简介</label>
                <textarea name="description" rows="4" maxlength="1000" placeholder="内容提要、适读人群、推荐理由等，将展示在读者门户">${esc(b.description || "")}</textarea>
                <span class="tip">最多 1000 字；门户书卡与检索详情会展示这段内容</span>
              </div>
            </div>`,
          confirmText: isEdit ? "保存修改" : "创建图书",
          onSubmit: async (form) => {
            const data = readForm(form);
            const result = isEdit ? await api.put(`/api/books/${id}`, data) : await api.post("/api/books", data);
            toast(
              isEdit ? "图书已更新" : "图书已创建",
              `《${result.title}》· 条码 ${result.barcode}`,
              "ok"
            );
            await Promise.all([loadBooks(), loadCategories(), state.view === "dashboard" ? loadDashboard() : null]);
          },
        });
      })
      .catch(showError);
  }

  /* ---------------- 图书详情与条码标签 ---------------- */

  function openBookDetail(id) {
    api
      .get(`/api/books/${id}`)
      .then((b) => {
        const avail = Number(b.available_copies) || 0;
        const total = Number(b.total_copies) || 0;
        const offShelf = !!b.off_shelf || total === 0;
        const tone = avail === 0 ? "danger" : avail <= 1 ? "warn" : "ok";
        const stockBadge = offShelf
          ? '<span class="badge neutral">已下架</span>'
          : `<span class="badge ${tone}">可借 ${avail} / ${total} 册</span>`;
        const body = `
          <div class="book-detail">
            <div class="bd-head">
              <div class="bd-title">《${esc(b.title)}》</div>
              <div class="bd-sub">${esc(b.author || "佚名")} · ${esc(b.publisher || "未标注出版社")}${b.publish_year ? ` · ${esc(b.publish_year)} 年` : ""}</div>
              <div class="bd-tags">
                <span class="badge neutral">${esc(b.category)}</span>
                ${stockBadge}
                <span class="badge neutral">馆藏位置 ${esc(b.location || "待分配")}</span>
                ${Number(b.active_loans) > 0 ? `<span class="badge info">在借 ${Number(b.active_loans)} 册</span>` : ""}
              </div>
            </div>
            <div class="bd-barcode">
              ${b.barcode ? Barcode.svg(b.barcode, { height: 56 }) : '<div class="empty">尚未设置条码</div>'}
            </div>
          </div>
          <div class="bd-section">
            <h4>图书简介</h4>
            <p class="bd-desc">${b.description ? esc(b.description) : "暂无简介，可点击下方「编辑这本图书」补充。"}</p>
          </div>
          <div class="bd-meta">
            <div><span>馆藏条码</span><b class="mono">${esc(b.barcode || "—")}</b></div>
            <div><span>ISBN</span><b class="mono">${esc(b.isbn)}</b></div>
            <div><span>入库时间</span><b>${esc(fmtDate(b.created_at))}</b></div>
            <div><span>最近更新</span><b>${esc(fmtDate(b.updated_at))}</b></div>
          </div>
          <div class="bd-actions">
            <button type="button" class="btn sm" data-print>打印条码标签</button>
            ${can("book:write") ? `<button type="button" class="btn sm" data-edit>编辑这本图书</button>
            <button type="button" class="btn sm ${offShelf ? "" : "danger"}" data-shelf>${offShelf ? "重新上架" : "下架这本图书"}</button>` : ""}
          </div>`;
        openModal({
          title: `图书详情 · 《${b.title}》`,
          body,
          confirmText: "关闭",
          hideCancel: true,
          onSubmit: async () => {},
          onMount: (form) => {
            const printBtn = form.querySelector("[data-print]");
            if (printBtn) {
              printBtn.addEventListener("click", () => {
                closeModal();
                openLabelDialog(b.id);
              });
            }
            const editBtn = form.querySelector("[data-edit]");
            if (editBtn) {
              editBtn.addEventListener("click", () => {
                closeModal();
                openBookForm(b.id);
              });
            }
            const shelfBtn = form.querySelector("[data-shelf]");
            if (shelfBtn) {
              shelfBtn.addEventListener("click", () => {
                closeModal();
                toggleShelf(b.id, !offShelf, b.title);
              });
            }
          },
        });
      })
      .catch(showError);
  }

  const LABEL_SIZES = [
    { key: "62", label: "62mm × 40mm（标准）", width: "62mm" },
    { key: "50", label: "50mm × 30mm（中号）", width: "50mm" },
    { key: "38", label: "38mm × 25mm（小号）", width: "38mm" },
  ];

  function printBarcodeLabel(book, opts) {
    if (!book.barcode) {
      toast("无法打印", "该书还没有条码号，请先编辑补充", "error");
      return;
    }
    const options = opts || {};
    const copies = Math.max(1, Math.min(100, Number(options.copies) || 1));
    const size = LABEL_SIZES.find((s) => s.key === String(options.size || "62")) || LABEL_SIZES[0];
    const one = `
      <div class="print-label" style="width:${size.width}">
        <div class="pl-lib">${esc(LIBRARY_NAME)}</div>
        <div class="pl-title">《${esc(book.title)}》</div>
        <div class="pl-sub">${esc(book.author || "佚名")} · 索书号 ${esc(book.location || "待分配")}</div>
        <div class="pl-code">${Barcode.svg(book.barcode, { height: 44, fontSize: 11 })}</div>
        <div class="pl-isbn">ISBN ${esc(book.isbn)}</div>
      </div>`;
    $("#printLayer").innerHTML = new Array(copies).fill(one).join("");
    window.print();
  }

  /** 标签打印弹窗：份数默认等于总册数（一册一签），尺寸三档可选。 */
  function openLabelDialog(bookId) {
    api
      .get(`/api/books/${bookId}`)
      .then((b) => {
        const total = Math.max(1, Number(b.total_copies) || 1);
        openModal({
          title: `打印条码标签 · 《${b.title}》`,
          body: `
            <div class="form-grid">
              <div class="field full"><label>打印份数<span class="req">*</span></label>
                <input name="copies" value="${total}" inputmode="numeric" autocomplete="off" />
                <span class="tip">默认按总册数 ${total} 份，一册一签；可填 1–100</span>
              </div>
              <div class="field full"><label>标签尺寸</label>
                <select name="size">
                  ${LABEL_SIZES.map((s) => `<option value="${esc(s.key)}">${esc(s.label)}</option>`).join("")}
                </select>
              </div>
              <div class="form-note">
                ${b.barcode ? `馆藏条码 <b>${esc(b.barcode)}</b>，将打印在标签下方。` : "该书尚未设置馆藏条码，请先编辑补充后再打印。"}
              </div>
            </div>`,
          confirmText: "打开打印",
          onSubmit: async (form) => {
            if (!b.barcode) {
              throw new ApiError("该书还没有条码号，请先编辑补充", 400, "validation_error", "copies");
            }
            const copies = Number(form.copies.value);
            if (!Number.isFinite(copies) || copies < 1 || copies > 100) {
              throw new ApiError("打印份数需在 1–100 之间", 400, "validation_error", "copies");
            }
            printBarcodeLabel(b, { copies, size: form.size.value });
            toast("已发送到打印", `《${b.title}》共 ${copies} 份标签`, "ok");
          },
        });
      })
      .catch(showError);
  }

  function confirmDeleteBook(id, title) {
    const label = title ? `《${title}》` : `图书 #${id}`;
    openModal({
      title: "删除图书",
      body: `<p>确定要删除 ${esc(label)} 吗？</p>
             <div class="form-note">该书若存在历史借阅记录将无法删除，会提示改用「下架」处理（下架后不再对读者展示，档案仍保留）。</div>`,
      confirmText: "确认删除",
      danger: true,
      onSubmit: async () => {
        await api.del(`/api/books/${id}`);
        toast("图书已删除", label, "ok");
        await Promise.all([loadBooks(), loadCategories()]);
      },
    });
  }

  /** 下架（total_copies=0）或重新上架（填写册数）。 */
  function toggleShelf(id, turnOff, title) {
    const label = title ? `《${title}》` : `图书 #${id}`;

    if (turnOff) {
      openModal({
        title: "下架图书",
        body: `<p>确定将 ${esc(label)} 下架吗？</p>
               <div class="form-note">下架后该书不再出现在读者门户，也不能再借出；若仍有未归还的册数需先办理还书。历史借阅记录完整保留，可随时重新上架。</div>`,
        confirmText: "确认下架",
        danger: true,
        onSubmit: async () => {
          const result = await api.post(`/api/books/${id}/shelf`, { total_copies: 0 });
          toast("图书已下架", `《${result.title}》已从读者门户移除`, "ok");
          await Promise.all([loadBooks(), loadCategories(), state.view === "dashboard" ? loadDashboard() : null]);
        },
      });
      return;
    }

    openModal({
      title: "重新上架",
      body: `<div class="form-grid">
               <div class="field full"><label>上架册数<span class="req">*</span></label>
                 <input name="total_copies" value="1" inputmode="numeric" />
                 <span class="tip">可借数将等于该册数；若仍有在外借出的册数，填写值不能小于在借数</span>
               </div>
             </div>`,
      confirmText: "确认上架",
      onSubmit: async (form) => {
        const picked = await api.post(`/api/books/${id}/shelf`, {
          total_copies: form.total_copies.value,
        });
        toast("图书已上架", `《${picked.title}》可借 ${picked.available_copies} / ${picked.total_copies} 册`, "ok");
        await Promise.all([loadBooks(), loadCategories(), state.view === "dashboard" ? loadDashboard() : null]);
      },
    });
  }

  /* ---------------- 读者视图 ---------------- */

  async function loadMembers() {
    const s = state.members;
    await withViewLoading("members", async () => {
      const payload = await api.get("/api/members", {
        keyword: s.keyword,
        status: s.status,
        sort: s.sort,
        order: s.order,
        page: s.page,
        page_size: s.pageSize,
      });
      const table = $("#memberTable");
      if (!payload.items.length) {
        const hasFilter = Boolean(s.keyword || s.status);
        table.innerHTML = emptyState(9, "没有匹配的读者", hasFilter ? "清除筛选条件" : "");
        const clear = table.querySelector("[data-clear]");
        if (clear) clear.addEventListener("click", resetMembers);
      } else {
        table.innerHTML = `
          <thead><tr>
            ${sortHeader("members", "name", "姓名 / 证号")}${sortHeader("members", "member_type", "类型")}<th>联系方式</th>${sortHeader("members", "max_borrow", "可借上限")}${sortHeader("members", "active_loans", "在借")}${sortHeader("members", "overdue_loans", "逾期")}${sortHeader("members", "status", "状态")}<th>门户账号</th><th>操作</th>
          </tr></thead>
          <tbody>${payload.items
            .map((m) => {
              const overdue = Number(m.overdue_loans) || 0;
              const active = Number(m.active_loans) || 0;
              const frozen = m.status === "frozen";
              return `<tr>
                <td class="title-cell"><strong>${esc(m.name)}</strong><div class="sub">${esc(m.card_no)}</div></td>
                <td><span class="badge neutral">${esc(m.member_type)}</span></td>
                <td>${esc(m.phone || "—")}<div class="sub">${esc(m.email || "")}</div></td>
                <td class="num">${Number(m.max_borrow) || 0}</td>
                <td class="num">${active}</td>
                <td>${overdue > 0 ? `<span class="badge danger">${overdue}</span>` : `<span class="sub">0</span>`}</td>
                <td><span class="badge ${frozen ? "warn" : "ok"}">${frozen ? "已冻结" : "正常"}</span></td>
                <td>${Number(m.has_account) ? '<span class="badge neutral">已开通</span>' : '<span class="sub">未开通</span>'}</td>
                <td><div class="actions">
                  ${can("member:write") ? `<button class="btn sm" data-edit="${m.id}">编辑</button>` : ""}
                  <button class="btn sm" data-history="${m.id}" data-name="${esc(m.name)}" data-card="${esc(m.card_no)}">借阅记录</button>
                  ${can("member:write") ? `<button class="btn sm" data-resetpw="${m.id}" data-name="${esc(m.name)}" data-card="${esc(m.card_no)}">重置密码</button>
                  <button class="btn sm danger" data-del="${m.id}" data-name="${esc(m.name)}" data-card="${esc(m.card_no)}">删除</button>` : ""}
                </div></td>
              </tr>`;
            })
            .join("")}</tbody>`;
      }

      renderPager($("#memberPager"), payload, (patch) => updateFilters(patch));

      table.querySelectorAll("[data-edit]").forEach((btn) =>
        btn.addEventListener("click", () => openMemberForm(Number(btn.dataset.edit)))
      );
      table.querySelectorAll("[data-del]").forEach((btn) =>
        btn.addEventListener("click", () =>
          confirmDeleteMember(Number(btn.dataset.del), btn.dataset.name, btn.dataset.card)
        )
      );
      table.querySelectorAll("[data-history]").forEach((btn) =>
        btn.addEventListener("click", () =>
          openMemberHistory(Number(btn.dataset.history), btn.dataset.name, btn.dataset.card)
        )
      );
      table.querySelectorAll("[data-resetpw]").forEach((btn) =>
        btn.addEventListener("click", () =>
          confirmResetPassword(Number(btn.dataset.resetpw), btn.dataset.name, btn.dataset.card)
        )
      );
    });
  }

  function resetMembers() {
    state.members = { page: 1, pageSize: state.members.pageSize, keyword: "", status: "", sort: "", order: "" };
    $("#memberKeyword").value = "";
    $("#memberStatus").value = "";
    syncUrl();
    loadMembers().catch(showError);
  }

  function confirmResetPassword(id, name, cardNo) {
    const label = name ? `${name}（${cardNo || "无证号"}）` : `读者 #${id}`;
    openModal({
      title: "重置读者门户密码",
      body: `<div class="form-note">将把读者 <b>${esc(label)}</b> 的登录密码重置为 <b>证号后 6 位</b>。重置后请转告读者，并提醒其登录后自行修改密码。</div>`,
      confirmText: "确认重置",
      onSubmit: async () => {
        const result = await api.post(`/api/members/${id}/reset-password`, {});
        toast("密码已重置", `${result.card_no} 的新密码：${result.initial_password}`, "ok");
        await loadMembers();
      },
    });
  }

  function openMemberForm(id) {
    const isEdit = !!id;
    const load = isEdit ? api.get(`/api/members/${id}`) : Promise.resolve(null);
    load
      .then((member) => {
        const m = member || {};
        const type = m.member_type || "学生";
        const status = m.status || "active";
        openModal({
          title: isEdit ? `编辑读者 · ${m.name || ""}（${m.card_no || id}）` : "新增读者",
          body: `
            <div class="form-grid">
              <div class="field"><label>读者证号<span class="req">*</span></label><input name="card_no" value="${esc(m.card_no || "")}" placeholder="2026001" /></div>
              <div class="field"><label>姓名<span class="req">*</span></label><input name="name" value="${esc(m.name || "")}" /></div>
              <div class="field"><label>读者类型<span class="req">*</span></label>
                <select name="member_type">
                  ${["学生", "教师", "社会", "VIP"].map((t) => `<option ${t === type ? "selected" : ""}>${t}</option>`).join("")}
                </select>
              </div>
              <div class="field"><label>状态<span class="req">*</span></label>
                <select name="status">
                  <option value="active" ${status === "active" ? "selected" : ""}>正常</option>
                  <option value="frozen" ${status === "frozen" ? "selected" : ""}>冻结</option>
                </select>
                <span class="tip">${isEdit ? `在借 ${m.active_loans || 0} 册，未还清不可冻结` : ""}</span>
              </div>
              <div class="field"><label>手机号</label><input name="phone" value="${esc(m.phone || "")}" /></div>
              <div class="field"><label>邮箱</label><input name="email" value="${esc(m.email || "")}" /></div>
              <div class="field full"><label>可借上限</label><input name="max_borrow" value="${m.max_borrow !== undefined ? esc(m.max_borrow) : ""}" placeholder="留空使用默认值" /></div>
            </div>`,
          confirmText: isEdit ? "保存修改" : "创建读者",
          onSubmit: async (form) => {
            const data = readForm(form);
            if (data.max_borrow === "") delete data.max_borrow;
            const result = isEdit ? await api.put(`/api/members/${id}`, data) : await api.post("/api/members", data);
            if (!isEdit && result.initial_password) {
              toast(
                "读者已创建，门户账号已开通",
                `${result.card_no} 的初始密码为 ${result.initial_password}（证号后 6 位），请转告读者`,
                "ok"
              );
            } else {
              toast(isEdit ? "读者已更新" : "读者已创建", `${result.name}（${result.card_no}）`, "ok");
            }
            await loadMembers();
          },
        });
      })
      .catch(showError);
  }

  function confirmDeleteMember(id, name, cardNo) {
    const label = name ? `${name}（${cardNo || "无证号"}）` : `读者 #${id}`;
    openModal({
      title: "删除读者",
      body: `<p>确定要删除读者 <b>${esc(label)}</b> 吗？</p>
             <div class="form-note">存在未还图书或历史借阅记录的读者不可删除（档案需保留）。</div>`,
      confirmText: "确认删除",
      danger: true,
      onSubmit: async () => {
        await api.del(`/api/members/${id}`);
        toast("读者已删除", label, "ok");
        await loadMembers();
      },
    });
  }

  function openMemberHistory(id, name, cardNo) {
    const label = name ? `${name}（${cardNo || "无证号"}）` : `读者 #${id}`;
    api
      .get("/api/loans", { member_id: id, status: "all", page: 1, page_size: 50 })
      .then((payload) => {
        const rows = payload.items || [];
        const active = rows.filter((r) => r.status !== "returned");
        const body = rows.length
          ? `<div class="summary-list">
              <div class="summary-row"><span class="k">借阅总数</span><span class="v">${payload.total} 条</span></div>
              <div class="summary-row"><span class="k">未归还</span><span class="v">${active.length} 册</span></div>
              <div class="summary-row"><span class="k">累计罚金</span><span class="v">${money(rows.reduce((sum, r) => sum + Number(r.fine || 0), 0))}</span></div>
            </div>
            <div class="table-scroll" style="margin-top:12px;border:1px solid var(--border);border-radius:8px">
              <table class="table">
                <thead><tr><th>图书</th><th>借出</th><th>应还</th><th>归还</th><th>状态</th></tr></thead>
                <tbody>${rows
                  .map(
                    (r) => `<tr>
                      <td class="wrap title-cell">${esc(r.book_title)}<div class="sub">${esc(r.book_barcode || "—")}</div></td>
                      <td class="sub">${esc(fmtDate(r.borrowed_at))}</td>
                      <td class="sub">${esc(fmtDate(r.due_at))}</td>
                      <td class="sub">${esc(fmtDate(r.returned_at))}</td>
                      <td>${loanBadge(r)}</td>
                    </tr>`
                  )
                  .join("")}</tbody>
              </table>
            </div>`
          : `<div class="empty">该读者暂无借阅记录</div>`;
        openModal({ title: `借阅记录 · ${label}`, body, confirmText: "关闭", hideCancel: true, onSubmit: async () => {} });
      })
      .catch(showError);
  }

  /* ---------------- 借阅视图 ---------------- */

  function loanBadge(r) {
    if (r.status === "returned") return `<span class="badge neutral">已归还</span>`;
    if (r.status === "overdue") return `<span class="badge danger">逾期 ${r.overdue_days} 天</span>`;
    return `<span class="badge info">在借</span>`;
  }

  async function loadLoans() {
    const s = state.loans;
    await withViewLoading("loans", async () => {
      const payload = await api.get("/api/loans", {
        keyword: s.keyword,
        status: s.status,
        sort: s.sort,
        order: s.order,
        page: s.page,
        page_size: s.pageSize,
      });
      const table = $("#loanTable");
      if (!payload.items.length) {
        const hasFilter = Boolean(s.keyword || (s.status && s.status !== "all"));
        table.innerHTML = emptyState(10, "没有匹配的借阅记录", hasFilter ? "清除筛选条件" : "");
        const clear = table.querySelector("[data-clear]");
        if (clear) clear.addEventListener("click", resetLoans);
      } else {
        const openCount = payload.items.filter((r) => r.status !== "returned").length;
        table.innerHTML = `
          <thead><tr>
            <th class="pick"><input type="checkbox" id="loanSelectAll" aria-label="全选当前页可还记录"${openCount ? "" : " disabled"} /></th>
            ${sortHeader("loans", "book_title", "图书")}${sortHeader("loans", "member_name", "读者")}${sortHeader("loans", "borrowed_at", "借出日期")}${sortHeader("loans", "due_at", "应还日期")}${sortHeader("loans", "returned_at", "归还日期")}${sortHeader("loans", "renew_count", "续借")}${sortHeader("loans", "status", "状态")}${sortHeader("loans", "fine", "罚金")}<th>操作</th>
          </tr></thead>
          <tbody>${payload.items
            .map((r) => {
              const open = r.status !== "returned";
              const pick = open && can("loan:write")
                ? `<input type="checkbox" class="loan-pick" value="${r.id}"
                          data-book="${esc(r.book_title)}" data-member="${esc(r.member_name)}"
                          data-card="${esc(r.member_card)}" data-due="${esc(fmtDate(r.due_at))}"
                          aria-label="选择《${esc(r.book_title)}》" />`
                : "";
              return `<tr>
                <td class="pick">${pick}</td>
                <td class="wrap title-cell"><strong>${esc(r.book_title)}</strong><div class="sub">${esc(r.book_barcode || "—")} · ISBN ${esc(r.book_isbn)}</div></td>
                <td>${esc(r.member_name)}<div class="sub">${esc(r.member_card)}</div></td>
                <td class="sub">${esc(fmtDate(r.borrowed_at))}</td>
                <td class="${r.status === "overdue" ? "" : "sub"}">${esc(fmtDate(r.due_at))}</td>
                <td class="sub">${esc(fmtDate(r.returned_at))}</td>
                <td class="num">${Number(r.renew_count) || 0}</td>
                <td>${loanBadge(r)}</td>
                <td class="num">${Number(r.fine) > 0
                  ? money(r.fine) + (r.fine_capped ? '<div class="sub">已封顶</div>' : "")
                  : '<span class="sub">—</span>'}</td>
                <td><div class="actions">
                  ${can("loan:write") ? `<button class="btn sm primary" data-return="${r.id}" ${open ? "" : "disabled"}
                          data-book="${esc(r.book_title)}" data-member="${esc(r.member_name)}" data-card="${esc(r.member_card)}">还书</button>
                  <button class="btn sm" data-renew="${r.id}" ${r.can_renew ? "" : "disabled"}
                          data-book="${esc(r.book_title)}" data-member="${esc(r.member_name)}" data-due="${esc(fmtDate(r.due_at))}">续借</button>` : '<span class="sub">仅可查看</span>'}
                </div></td>
              </tr>`;
            })
            .join("")}</tbody>`;
      }

      renderPager($("#loanPager"), payload, (patch) => updateFilters(patch));

      table.querySelectorAll("[data-return]").forEach((btn) =>
        btn.addEventListener("click", () =>
          doReturn(Number(btn.dataset.return), {
            book: btn.dataset.book,
            member: btn.dataset.member,
            card: btn.dataset.card,
          })
        )
      );
      table.querySelectorAll("[data-renew]").forEach((btn) =>
        btn.addEventListener("click", () =>
          doRenew(Number(btn.dataset.renew), {
            book: btn.dataset.book,
            member: btn.dataset.member,
            due: btn.dataset.due,
          })
        )
      );

      // 批量勾选（仅当前页；翻页后重新选择，避免跨页误还）
      table.querySelectorAll(".loan-pick").forEach((box) => box.addEventListener("change", updateSelection));
      const selectAll = table.querySelector("#loanSelectAll");
      if (selectAll) {
        selectAll.addEventListener("change", () => {
          table.querySelectorAll(".loan-pick").forEach((box) => { box.checked = selectAll.checked; });
          updateSelection();
        });
      }
      updateSelection();
    });
  }

  function resetLoans() {
    state.loans = { page: 1, pageSize: state.loans.pageSize, keyword: "", status: "all", sort: "", order: "" };
    $("#loanKeyword").value = "";
    document.querySelectorAll("#loanTabs .tab").forEach((t) =>
      t.classList.toggle("is-active", t.dataset.status === "all")
    );
    syncUrl();
    loadLoans().catch(showError);
  }

  /** 同步「批量还书」按钮与全选框状态（选中数 / 半选）。 */
  function updateSelection() {
    const table = $("#loanTable");
    const boxes = table ? Array.from(table.querySelectorAll(".loan-pick")) : [];
    const checked = boxes.filter((b) => b.checked);
    const selectAll = table && table.querySelector("#loanSelectAll");
    if (selectAll) {
      selectAll.checked = boxes.length > 0 && checked.length === boxes.length;
      selectAll.indeterminate = checked.length > 0 && checked.length < boxes.length;
    }
    const btn = $("#loanReturnBatch");
    if (btn) {
      btn.disabled = checked.length === 0;
      btn.textContent = checked.length ? `批量还书（${checked.length}）` : "批量还书";
    }
    const count = $("#loanSelCount");
    if (count) count.textContent = checked.length ? `已选 ${checked.length} 册` : "";
  }

  function openBatchReturn() {
    const table = $("#loanTable");
    const picks = table ? Array.from(table.querySelectorAll(".loan-pick:checked")) : [];
    if (!picks.length) {
      toast("未选择记录", "请先勾选要归还的图书", "warn");
      return;
    }
    const items = picks.map((b) => ({
      id: Number(b.value),
      book: b.dataset.book,
      member: b.dataset.member,
      card: b.dataset.card,
    }));
    const preview = items
      .slice(0, 6)
      .map(
        (i) =>
          `<div class="summary-row"><span class="k">《${esc(i.book)}》</span><span class="v">${esc(i.member)}${i.card ? `（${esc(i.card)}）` : ""}</span></div>`
      )
      .join("");
    const more = items.length > 6
      ? `<div class="summary-row"><span class="k">其余</span><span class="v">${items.length - 6} 册</span></div>`
      : "";
    openModal({
      title: `批量还书（${items.length} 册）`,
      body: `<p>确认一次性办理以下 <b>${items.length}</b> 册图书的还书手续？</p>
             <div class="summary-list" style="margin-top:10px">${preview}${more}</div>
             <div class="form-note" style="margin-top:10px">系统将逐册归还并结算罚金；个别记录若已归还或不存在会被跳过，并在结果中列出。</div>`,
      confirmText: `确认归还 ${items.length} 册`,
      onSubmit: async () => {
        const result = await api.post("/api/loans/return-batch", { ids: items.map((i) => i.id) });
        await refreshAfterLoanChange();
        // 等确认弹窗关闭后再展示明细，避免被 closeModal 覆盖
        setTimeout(() => showBatchResult(result), 0);
      },
    });
  }

  function showBatchResult(result) {
    const ok = Number(result.succeeded_count) || 0;
    const bad = Number(result.failed_count) || 0;
    const fine = Number(result.fine_total) || 0;
    if (bad === 0) {
      toast(
        "批量还书完成",
        `成功归还 ${ok} 册${fine > 0 ? `，结算罚金 ${money(fine)}` : ""}`,
        "ok"
      );
      return;
    }
    const failedRows = (result.failed || [])
      .map((f) => `<tr><td class="num">#${f.id}</td><td class="wrap">${esc(f.message)}</td></tr>`)
      .join("");
    openModal({
      title: `批量还书结果：成功 ${ok} 册 / 未处理 ${bad} 册`,
      body: `<div class="summary-list">
          <div class="summary-row"><span class="k">成功归还</span><span class="v">${ok} 册</span></div>
          <div class="summary-row"><span class="k">未处理</span><span class="v">${bad} 册</span></div>
          <div class="summary-row"><span class="k">结算罚金</span><span class="v">${money(fine)}</span></div>
        </div>
        <div class="bd-section"><h4>未处理的记录</h4>
          <div class="table-scroll" style="border:1px solid var(--border);border-radius:8px">
            <table class="table"><thead><tr><th>记录</th><th>原因</th></tr></thead><tbody>${failedRows}</tbody></table>
          </div>
        </div>`,
      confirmText: "关闭",
      hideCancel: true,
      onSubmit: async () => {},
    });
    toast("批量还书完成", `成功 ${ok} 册，未处理 ${bad} 册`, "warn");
  }

  function doReturn(id, info) {
    const ctx = info || {};
    const label = ctx.book ? `《${esc(ctx.book)}》` : `借阅记录 #${id}`;
    const reader = ctx.member ? `读者 ${esc(ctx.member)}${ctx.card ? `（${esc(ctx.card)}）` : ""}` : "";
    openModal({
      title: "办理还书",
      body: `<p>确认办理 ${label}${reader ? ` · ${reader}` : ""} 的还书手续？</p>
             <div class="form-note">系统将恢复该书库存并结算罚金：未逾期不产生罚金；逾期按每日标准累计，单笔罚金有配置上限，不会无限累加。</div>`,
      confirmText: "确认还书",
      onSubmit: async () => {
        const result = await api.post(`/api/loans/${id}/return`, {});
        const extra = Number(result.fine) > 0 ? `逾期 ${result.overdue_days} 天，罚金 ${money(result.fine)}` : "按期归还，无罚金";
        toast("还书成功", `《${result.book_title}》· 条码 ${result.book_barcode || "—"} · ${extra}`, Number(result.fine) > 0 ? "warn" : "ok");
        await refreshAfterLoanChange();
      },
    });
  }

  function doRenew(id, info) {
    const ctx = info || {};
    const label = ctx.book ? `《${esc(ctx.book)}》` : `借阅记录 #${id}`;
    const reader = ctx.member ? `读者 ${esc(ctx.member)}` : "";
    openModal({
      title: "办理续借",
      body: `<p>确认为 ${label}${reader ? ` · ${reader}` : ""} 续借一个完整借期？</p>
             <div class="form-note">续借会在原应还日期${ctx.due && ctx.due !== "—" ? `（${esc(ctx.due)}）` : ""}基础上顺延；已逾期或达到续借上限的记录无法续借。</div>`,
      confirmText: "确认续借",
      onSubmit: async () => {
        const result = await api.post(`/api/loans/${id}/renew`, {});
        toast("续借成功", `《${result.book_title}》应还日期顺延至 ${fmtDate(result.due_at)}`, "ok");
        await refreshAfterLoanChange();
      },
    });
  }

  async function refreshAfterLoanChange() {
    const jobs = [loadLoans(), loadBooks()];
    if (state.view === "dashboard") jobs.push(loadDashboard());
    await Promise.all(jobs);
  }

  /* ---------------- 借书弹窗 ---------------- */

  function openBorrowForm(prefillBookId) {
    openModal({
      title: "办理借书",
      body: `
        <div class="form-grid">
          <div class="field full"><label>扫码 / 馆藏条码</label>
            <input name="scan" id="borrowScan" autocomplete="off"
                   placeholder="扫码枪扫条码，或手输条码后回车（自动选中唯一命中）" />
            <span class="tip">扫码枪扫完会自动回车；系统按条码精确匹配并选中图书，再扫不中会明确提示原因</span>
          </div>
          <div class="field full"><label>图书<span class="req">*</span></label>
            <input name="book_filter" autocomplete="off" placeholder="书名 / 作者 / ISBN / 条码 过滤（只剩一本时自动选中）" />
            <select name="book_id" id="borrowBook"></select>
            <span class="tip" id="borrowBookHint"></span>
          </div>
          <div class="field full"><label>读者<span class="req">*</span></label>
            <input name="member_filter" autocomplete="off" placeholder="姓名 / 证号 / 手机号 过滤（回车选中唯一命中）" />
            <select name="member_id" id="borrowMember"></select>
            <span class="tip" id="borrowMemberHint"></span>
          </div>
          <div class="field full"><label>备注</label><input name="note" placeholder="选填" /></div>
          <div class="form-note">图书列表只显示当前有可借库存的书。有逾期未还、已达可借上限或被冻结的读者无法办理借书。</div>
        </div>`,
      confirmText: "确认借出",
      onMount: async (form) => {
        const scanInput = $("#borrowScan", form);
        const bookFilter = form.book_filter;
        const memberFilter = form.member_filter;
        const bookSel = $("#borrowBook", form);
        const memberSel = $("#borrowMember", form);
        const bookHint = $("#borrowBookHint", form);
        const memberHint = $("#borrowMemberHint", form);

        const fillBooks = async (keyword) => {
          const payload = await api.get("/api/books", {
            keyword: keyword || "",
            available_only: "1",
            page: 1,
            page_size: 50,
          });
          const items = payload.items || [];
          bookSel.innerHTML =
            `<option value="">请选择图书</option>` +
            (items.length
              ? items
                  .map(
                    (b) => `<option value="${b.id}">${esc(
                      `${b.title}（${b.author || "佚名"}）· ${b.barcode || "无条码"} · 可借 ${b.available_copies}/${b.total_copies}`
                    )}</option>`
                  )
                  .join("")
              : `<option value="" disabled>没有可借图书</option>`);
          // 过滤后只剩一本时直接选中：扫码 / 搜索后无需再手点
          if (items.length === 1) bookSel.selectedIndex = 1;
          if (prefillBookId && items.some((b) => Number(b.id) === prefillBookId)) {
            bookSel.value = String(prefillBookId);
          }
          bookHint.textContent = items.length
            ? `${items.length} 本可借${keyword ? `（关键词：${keyword}）` : ""}`
            : "没有匹配的可借图书";
          return items;
        };

        const fillMembers = async (keyword) => {
          const payload = await api.get("/api/members", { keyword: keyword || "", page: 1, page_size: 50 });
          const items = (payload.items || []).filter((m) => m.status === "active");
          memberSel.innerHTML =
            `<option value="">请选择读者</option>` +
            (items.length
              ? items
                  .map((m) => {
                    const overdue = Number(m.overdue_loans) || 0;
                    const full = Number(m.active_loans) >= Number(m.max_borrow);
                    const flag = overdue > 0 ? " · 有逾期" : full ? " · 已满额" : "";
                    return `<option value="${m.id}">${esc(
                      `${m.name}（${m.card_no}）· 在借 ${m.active_loans}/${m.max_borrow}${flag}`
                    )}</option>`;
                  })
                  .join("")
              : `<option value="" disabled>没有可借书的读者</option>`);
          if (items.length === 1) memberSel.selectedIndex = 1;
          memberHint.textContent = keyword ? (items.length ? `匹配 ${items.length} 位读者` : "没有匹配的读者") : "";
          return items;
        };

        // 扫码 / 输条码：精确匹配优先，命中后自动选中并把焦点交给读者
        const handleScan = async (raw) => {
          const code = String(raw || "").trim();
          if (!code) {
            scanInput.focus();
            return;
          }
          const payload = await api.get("/api/books", { keyword: code, page: 1, page_size: 20 });
          const items = payload.items || [];
          const target =
            items.find((b) => String(b.barcode || "").toUpperCase() === code.toUpperCase()) ||
            (items.length === 1 ? items[0] : null);
          if (!target) {
            toast("未找到该条码", `没有条码为 ${code} 的图书，请核对后再扫`, "error");
            scanInput.select();
            return;
          }
          if (!Number(target.available_copies)) {
            const reason = target.off_shelf ? "已下架" : "当前全部借出";
            toast("无可借库存", `《${target.title}》${reason}，无法借出`, "error");
            scanInput.select();
            return;
          }
          await fillBooks(target.barcode);
          bookSel.value = String(target.id);
          scanInput.value = "";
          scanInput.blur();
          memberFilter.focus();
        };

        const selectedBook = () =>
          bookSel.value && bookSel.options.length && Number(bookSel.value) ? bookSel.value : "";
        const selectedMember = () =>
          memberSel.value && memberSel.options.length && Number(memberSel.value) ? memberSel.value : "";

        const onBookFilter = debounce((value) => fillBooks(value).catch(showError), 260);
        const onMemberFilter = debounce((value) => fillMembers(value).catch(showError), 260);
        bookFilter.addEventListener("input", (e) => onBookFilter(e.target.value));
        memberFilter.addEventListener("input", (e) => onMemberFilter(e.target.value));

        // 扫码枪以 Enter 结尾，表单默认 submit 已被拦截 —— 这里显式接管回车链路
        form.addEventListener("keydown", (e) => {
          if (e.key !== "Enter") return;
          e.preventDefault();
          const target = e.target;
          if (target === scanInput) {
            handleScan(scanInput.value).catch(showError);
            return;
          }
          if (target === bookFilter) {
            memberFilter.focus();
            return;
          }
          if (target === memberFilter) {
            if (!selectedMember()) return;
          }
          if (selectedBook() && selectedMember()) {
            const confirmBtn = $("#modalFoot [data-confirm]");
            if (confirmBtn) confirmBtn.click();
          } else {
            (selectedBook() ? memberFilter : scanInput).focus();
          }
        });

        await Promise.all([fillBooks(""), fillMembers("")]);

        if (prefillBookId) memberFilter.focus();
        else scanInput.focus();
      },
      onSubmit: async (form) => {
        const bookId = form.book_id.value;
        const memberId = form.member_id.value;
        if (!bookId) throw new ApiError("请选择要借出的图书", 400, "validation_error", "book_id");
        if (!memberId) throw new ApiError("请选择借阅读者", 400, "validation_error", "member_id");

        const result = await api.post("/api/loans/borrow", {
          book_id: Number(bookId),
          member_id: Number(memberId),
          note: form.note.value,
        });
        toast("借书成功", `《${result.book_title}》· ${result.book_barcode || ""} → ${result.member_name}，应还 ${fmtDate(result.due_at)}`, "ok");
        await refreshAfterLoanChange();
      },
    });
  }

  /* ---------------- Modal ---------------- */

  // 可聚焦元素：用于焦点陷阱与「打开后聚焦哪里」
  const FOCUSABLE_SELECTOR =
    'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), ' +
    'select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

  function focusableIn(container) {
    return Array.from(container.querySelectorAll(FOCUSABLE_SELECTOR)).filter(
      (el) => el.offsetWidth > 0 || el.offsetHeight > 0 || el === document.activeElement
    );
  }

  /** Tab / Shift+Tab 只在弹窗内循环，避免焦点跑到背后的页面上。 */
  function trapTab(e) {
    const modal = $("#modal");
    const items = focusableIn(modal);
    if (!items.length) {
      e.preventDefault();
      return;
    }
    const first = items[0];
    const last = items[items.length - 1];
    const active = document.activeElement;
    if (e.shiftKey) {
      if (active === first || !modal.contains(active)) {
        e.preventDefault();
        last.focus();
      }
    } else if (active === last || !modal.contains(active)) {
      e.preventDefault();
      first.focus();
    }
  }

  /** 表单可访问性：给每个 .field 的 label 与控件建立 for/id 关联。
   *  模板无需逐个手写 id —— 在这里统一补，避免漏掉某一个字段。 */
  function linkLabels(form) {
    const seen = new Set();
    form.querySelectorAll("input[id], select[id], textarea[id]").forEach((el) => seen.add(el.id));
    form.querySelectorAll(".field").forEach((field) => {
      const label = field.querySelector("label");
      const ctl =
        field.querySelector('input:not([name$="_filter"]), select, textarea') ||
        field.querySelector("input, select, textarea");
      if (!label || !ctl || label.htmlFor) return;
      let id = ctl.id;
      if (!id) {
        const base = String(ctl.getAttribute("name") || "field").replace(/[^A-Za-z0-9_-]/g, "") || "field";
        id = "mf_" + base;
        let n = 2;
        while (seen.has(id)) {
          id = `mf_${base}_${n}`;
          n += 1;
        }
        ctl.id = id;
      }
      seen.add(id);
      label.setAttribute("for", id);
    });
  }

  // 关闭弹窗后把焦点还给触发它的那个按钮（列表可能已重渲染，元素不在文档里就跳过）
  let lastFocusedElement = null;

  function openModal(opts) {
    const layer = $("#modalLayer");
    const modal = $("#modal");
    lastFocusedElement = document.activeElement;
    $("#modalTitle").textContent = opts.title || "";

    const body = $("#modalBody");
    body.innerHTML = "";
    const form = document.createElement("form");
    form.setAttribute("novalidate", "novalidate");
    form.addEventListener("submit", (e) => e.preventDefault());
    form.innerHTML = opts.body || "";
    body.appendChild(form);
    linkLabels(form);

    const foot = $("#modalFoot");
    foot.innerHTML =
      (opts.hideCancel ? "" : `<button class="btn" data-cancel>取消</button>`) +
      `<button class="btn ${opts.danger ? "danger" : "primary"}" data-confirm>${esc(opts.confirmText || "确定")}</button>`;

    const confirmBtn = foot.querySelector("[data-confirm]");
    confirmBtn.addEventListener("click", async () => {
      clearFieldErrors(form);
      confirmBtn.disabled = true;
      const original = confirmBtn.textContent;
      confirmBtn.textContent = "处理中…";
      try {
        await opts.onSubmit(form);
        closeModal();
      } catch (err) {
        showModalError(form, err);
      } finally {
        confirmBtn.disabled = false;
        confirmBtn.textContent = original;
      }
    });

    const cancelBtn = foot.querySelector("[data-cancel]");
    if (cancelBtn) cancelBtn.addEventListener("click", closeModal);

    layer.hidden = false;

    // 焦点进入弹窗：优先第一个输入项 → 其次「取消」→ 关闭按钮。
    // 破坏性确认框刻意不聚焦「确认」按钮，避免误按回车直接执行。
    const field = form.querySelector(
      'input:not([type="hidden"]):not([disabled]), select:not([disabled]), textarea:not([disabled])'
    );
    const entry = field || cancelBtn || $("#modalClose") || focusableIn(modal)[0] || modal;
    try {
      entry.focus({ preventScroll: true });
    } catch (e) {
      try { entry.focus(); } catch (e2) { /* 忽略：无法聚焦时保持原焦点 */ }
    }

    if (opts.onMount) {
      // 挂载期间（下拉列表还在拉取）禁用确认，避免对着空下拉框点「确认借出」
      confirmBtn.disabled = true;
      Promise.resolve(opts.onMount(form))
        .catch(showError)
        .finally(() => {
          confirmBtn.disabled = false;
        });
    }
  }

  function closeModal() {
    const layer = $("#modalLayer");
    if (layer.hidden) return;
    layer.hidden = true;
    $("#modalBody").innerHTML = "";
    $("#modalFoot").innerHTML = "";
    const back = lastFocusedElement;
    lastFocusedElement = null;
    if (back && typeof back.focus === "function" && document.contains(back)) {
      try {
        back.focus({ preventScroll: true });
      } catch (e) {
        /* 元素已不可聚焦时忽略 */
      }
    }
  }

  function readForm(form) {
    const data = {};
    form.querySelectorAll("input[name], select[name], textarea[name]").forEach((el) => {
      if (el.name.endsWith("_filter")) return;
      data[el.name] = el.value;
    });
    return data;
  }

  function clearFieldErrors(scope) {
    scope.querySelectorAll(".has-error").forEach((el) => el.classList.remove("has-error"));
    scope.querySelectorAll(".err").forEach((el) => el.remove());
  }

  function showModalError(form, err) {
    const message = err && err.message ? err.message : "操作失败";
    let focusTarget = null;
    if (err && err.field) {
      const el = form.querySelector(`[name="${err.field}"]`);
      if (el) {
        const field = el.closest(".field");
        if (field) {
          field.classList.add("has-error");
          const tip = document.createElement("span");
          tip.className = "err";
          tip.textContent = message;
          field.appendChild(tip);
        }
        focusTarget = el;
      }
    }
    toast("操作未完成", message, "error");
    // 会话 / CSRF 失效：弹窗里的重试必然继续失败，直接给出刷新出口
    if (err && err.code === "csrf_failed" && !form.querySelector("[data-session-expired]")) {
      const tip = document.createElement("div");
      tip.className = "form-note";
      tip.setAttribute("data-session-expired", "1");
      tip.innerHTML =
        "页面会话已过期，继续重试不会成功。<button class=\"btn sm\" type=\"button\" data-refresh>立即刷新页面</button>";
      form.prepend(tip);
      tip.querySelector("[data-refresh]").addEventListener("click", () => location.reload());
    }
    // 长表单报错后把用户带到出错字段：弹窗内容区是滚动容器，滚动 + 聚焦一次到位
    if (focusTarget) {
      try {
        focusTarget.focus({ preventScroll: true });
      } catch (e) {
        focusTarget.focus();
      }
      if (focusTarget.scrollIntoView) focusTarget.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  }

  function showError(err) {
    toast("请求失败", (err && err.message) || String(err), "error");
  }

  /* ---------------- 会话 ---------------- */

  async function loadSession() {
    const data = await api.request("/api/auth/me");
    auth.admin = data.admin;
    auth.csrf = data.csrf_token;
    auth.permissions = data.permissions || [];
    const name = (auth.admin && (auth.admin.display_name || auth.admin.username)) || "管理员";
    $("#adminName").textContent = name;
    auth.roleLabel =
      data.role_label || (auth.admin.role === "admin" ? "系统管理员" : "流通馆员");
    $("#adminRole").textContent = auth.roleLabel;
    $("#adminAvatar").textContent = name.slice(0, 1);
    applyPermissions();
    renderSecurityBanner(data.security);
    renderPolicy(data.policy);
    // 会话到期提醒：闲置到临期前给出提示，避免弹窗里未保存的内容无声丢失
    sessionWatch.start(data.session && data.session.lifetime_seconds);
    return data;
  }

  /** 按当前角色收拢入口：无权的东西不显示，而不是"点了才报 403"。 */
  function applyPermissions() {
    document.querySelectorAll("[data-permission]").forEach((node) => {
      const needed = node.dataset.permission;
      const allowed = can(needed);
      if (node.classList.contains("nav-item") || node.classList.contains("view")) {
        node.hidden = !allowed;
      } else if (node.disabled !== undefined) {
        node.disabled = !allowed;
        node.hidden = !allowed;
      } else {
        node.hidden = !allowed;
      }
    });
    // 无权访问的视图不该停在页面上（例如刷新时 hash 还指着它）
    const current = VIEWS[state.view];
    if (current && current.permission && !can(current.permission)) {
      state.view = "dashboard";
    }
  }

  /* 默认口令提醒：只要管理员还在用初始口令，就一直挂在页面上，直到改掉为止 */
  function renderSecurityBanner(security) {
    const banner = $("#securityBanner");
    if (!banner) return;
    banner.hidden = !(security && security.default_admin_password);
  }

  async function doLogout() {
    try {
      await api.post("/api/auth/logout", {});
    } catch (err) {
      /* 忽略：无论服务端结果如何都回到登录页 */
    }
    location.replace("/");
  }

  function openChangePassword() {
    openModal({
      title: "修改密码",
      body: `
        <div class="form-grid">
          <div class="field full"><label>当前密码<span class="req">*</span></label><input name="old_password" type="password" autocomplete="current-password" /></div>
          <div class="field full"><label>新密码<span class="req">*</span></label><input name="new_password" type="password" autocomplete="new-password" /><span class="tip">至少 6 位</span></div>
          <div class="field full"><label>确认新密码<span class="req">*</span></label><input name="confirm_password" type="password" autocomplete="new-password" /></div>
          <div class="form-note">修改成功后当前会话会失效，需要用新密码重新登录。</div>
        </div>`,
      confirmText: "确认修改",
      onSubmit: async (form) => {
        const oldPassword = form.old_password.value;
        const newPassword = form.new_password.value;
        if (!oldPassword) throw new ApiError("请输入当前密码", 400, "validation_error", "old_password");
        if (!newPassword || newPassword.length < 6) throw new ApiError("新密码至少 6 位", 400, "validation_error", "new_password");
        if (newPassword !== form.confirm_password.value) {
          throw new ApiError("两次输入的新密码不一致", 400, "validation_error", "confirm_password");
        }
        await api.post("/api/auth/password", { old_password: oldPassword, new_password: newPassword });
        toast("密码已修改", "请使用新密码重新登录", "ok");
        setTimeout(() => location.replace("/"), 1200);
      },
    });
  }

  /* ---------------- 路由 / 导航 ---------------- */

  /** 只切换界面并加载数据，不改 URL（URL 由 navigateTo / hashchange 统一负责）。 */
  function activateView(view) {
    if (!VIEWS[view]) view = "dashboard";
    if (VIEWS[view].permission && !can(VIEWS[view].permission)) view = "dashboard";
    state.view = view;
    document.querySelectorAll(".nav-item").forEach((btn) =>
      btn.classList.toggle("is-active", btn.dataset.view === view)
    );
    document.querySelectorAll(".view").forEach((sec) =>
      sec.classList.toggle("is-active", sec.id === "view-" + view)
    );
    $("#pageTitle").textContent = VIEWS[view].title;
    $("#pageSub").textContent = VIEWS[view].sub;
    return refreshCurrentView();
  }

  /** 用户主动导航：写 hash（带上该视图上次的筛选），由 hashchange 接管后续。 */
  function navigateTo(view) {
    if (!VIEWS[view]) view = "dashboard";
    const next = buildHash(view);
    if (location.hash === next) {
      activateView(view).catch(showError);
      return;
    }
    location.hash = next;
  }

  /** hash 变化的唯一入口：首次加载、导航点击、浏览器前进/后退都走这里。 */
  function onHashChange() {
    const { view, params } = readHash(location.hash);
    let target = VIEWS[view] ? view : "dashboard";
    if (VIEWS[target].permission && !can(VIEWS[target].permission)) {
      // 手改地址栏 / 旧书签指向无权视图：挡回概览，并把地址栏改干净
      toast(
        "没有访问权限",
        `当前角色（${auth.roleLabel || "—"}）看不到「${VIEWS[target].title}」`,
        "warn"
      );
      target = "dashboard";
      history.replaceState(null, "", location.pathname + location.search + buildHash(target));
    } else if (target !== view) {
      // 非法 hash 归一为默认视图，并把地址栏改干净（不再新增历史条目）
      history.replaceState(null, "", location.pathname + location.search + buildHash(target));
    } else {
      applyHashParams(target, params);
    }
    activateView(target).catch(showError);
  }

  async function refreshCurrentView() {
    if (state.view === "dashboard") return loadDashboard();
    if (state.view === "books") return Promise.all([loadBooks(), loadCategories()]);
    if (state.view === "members") return loadMembers();
    if (state.view === "loans") return loadLoans();
    if (state.view === "accounts") return loadAccounts();
    if (state.view === "logs") return loadLogs();
  }

  async function loadDashboard() {
    const data = await api.get("/api/dashboard/overview");
    renderDashboard(data);
  }

  /* ---------------- 导出（按当前筛选条件） ---------------- */

  function exportBooks() {
    const s = state.books;
    return withExport($("#bookExport"), async () => {
      const rows = await fetchAllRows("/api/books", {
        keyword: s.keyword,
        category: s.category,
        available_only: s.availableOnly ? "1" : "",
        sort: s.sort,
        order: s.order,
      });
      if (!rows.length) {
        toast("没有可导出的数据", "当前筛选条件下没有图书", "warn");
        return;
      }
      downloadCsv(
        `图书清单-${fileStamp()}.csv`,
        ["书名", "作者", "ISBN", "馆藏条码", "分类", "出版社", "出版年", "馆藏位置", "总册数", "可借数", "状态"],
        rows.map((b) => {
          const total = Number(b.total_copies) || 0;
          return [
            b.title, b.author, b.isbn, b.barcode, b.category, b.publisher,
            b.publish_year, b.location, total, Number(b.available_copies) || 0,
            b.off_shelf || total === 0 ? "已下架" : "在架",
          ];
        })
      );
      toast("已导出图书清单", `共 ${rows.length} 条记录`, "ok");
    });
  }

  function exportMembers() {
    const s = state.members;
    return withExport($("#memberExport"), async () => {
      const rows = await fetchAllRows("/api/members", {
        keyword: s.keyword, status: s.status, sort: s.sort, order: s.order,
      });
      if (!rows.length) {
        toast("没有可导出的数据", "当前筛选条件下没有读者", "warn");
        return;
      }
      downloadCsv(
        `读者名册-${fileStamp()}.csv`,
        ["姓名", "借书证号", "读者类型", "手机号", "邮箱", "可借上限", "在借册数", "逾期册数", "状态", "门户账号"],
        rows.map((m) => [
          m.name, m.card_no, m.member_type, m.phone, m.email,
          Number(m.max_borrow) || 0, Number(m.active_loans) || 0, Number(m.overdue_loans) || 0,
          m.status === "frozen" ? "已冻结" : "正常",
          Number(m.has_account) ? "已开通" : "未开通",
        ])
      );
      toast("已导出读者名册", `共 ${rows.length} 条记录`, "ok");
    });
  }

  function exportLoans() {
    const s = state.loans;
    return withExport($("#loanExport"), async () => {
      const rows = await fetchAllRows("/api/loans", {
        keyword: s.keyword, status: s.status, sort: s.sort, order: s.order,
      });
      if (!rows.length) {
        toast("没有可导出的数据", "当前筛选条件下没有借阅记录", "warn");
        return;
      }
      downloadCsv(
        `借阅明细-${fileStamp()}.csv`,
        ["书名", "馆藏条码", "ISBN", "读者姓名", "借书证号", "借出日期", "应还日期", "归还日期", "续借次数", "状态", "逾期天数", "罚金(元)"],
        rows.map((r) => [
          r.book_title, r.book_barcode, r.book_isbn, r.member_name, r.member_card,
          fmtDate(r.borrowed_at), fmtDate(r.due_at), fmtDate(r.returned_at),
          Number(r.renew_count) || 0, r.status_label || r.status,
          Number(r.overdue_days) || 0, Number(r.fine) ? Number(r.fine).toFixed(2) : "0.00",
        ])
      );
      toast("已导出借阅明细", `共 ${rows.length} 条记录`, "ok");
    });
  }

  /* ---------------- 账号管理（仅系统管理员） ---------------- */

  const ROLE_LABELS = { admin: "系统管理员", librarian: "流通馆员" };

  function roleBadge(role) {
    const label = ROLE_LABELS[role] || role || "—";
    return `<span class="badge ${role === "admin" ? "info" : "neutral"}">${esc(label)}</span>`;
  }

  async function loadAccounts() {
    const s = state.accounts;
    await withViewLoading("accounts", async () => {
      const payload = await api.get("/api/accounts");
      renderAccounts(payload, s.keyword);
    });
  }

  function renderAccounts(payload, keyword) {
    const table = $("#accountTable");
    const kw = String(keyword || "").trim().toLowerCase();
    const items = (payload.items || []).filter((a) =>
      kw
        ? String(a.username || "").toLowerCase().includes(kw) ||
          String(a.display_name || "").toLowerCase().includes(kw)
        : true
    );
    if (!items.length) {
      table.innerHTML = emptyState(7, kw ? "没有匹配的后台账号" : "还没有后台账号", kw ? "清除搜索" : "");
      const clear = table.querySelector("[data-clear]");
      if (clear) clear.addEventListener("click", resetAccounts);
      return;
    }
    table.innerHTML = `<thead><tr>
        <th>用户名</th><th>姓名</th><th>角色</th><th>状态</th><th>最近登录</th><th>创建时间</th><th>操作</th>
      </tr></thead>
      <tbody>${items
        .map((a) => {
          const self = auth.admin && Number(auth.admin.id) === Number(a.id);
          return `<tr${a.is_active ? "" : ' class="is-inactive"'}>
            <td class="title-cell"><strong class="mono">${esc(a.username)}</strong>${
              self ? '<div class="sub">当前登录账号</div>' : ""
            }</td>
            <td>${esc(a.display_name || "—")}</td>
            <td>${roleBadge(a.role)}</td>
            <td>${
              a.is_active
                ? '<span class="badge ok">已启用</span>'
                : '<span class="badge warn">已停用</span>'
            }</td>
            <td class="sub">${esc(a.last_login_at ? fmtDateTime(a.last_login_at) : "从未登录")}</td>
            <td class="sub">${esc(fmtDate(a.created_at))}</td>
            <td><div class="actions">
              <button class="btn sm" data-edit="${a.id}">编辑</button>
              <button class="btn sm" data-resetpw="${a.id}" data-username="${esc(a.username)}">重置密码</button>
              <button class="btn sm danger" data-del="${a.id}" data-username="${esc(a.username)}"${
                self ? ' disabled title="不能删除自己的账号"' : ""
              }>删除</button>
            </div></td>
          </tr>`;
        })
        .join("")}</tbody>`;
  }

  function resetAccounts() {
    state.accounts.keyword = "";
    const box = $("#accountKeyword");
    if (box) box.value = "";
    syncUrl();
    loadAccounts().catch(showError);
  }

  function openAccountForm(accountId) {
    const isEdit = accountId !== null && accountId !== undefined;
    const build = (account) => {
      const roles =
        state.accountRoles && state.accountRoles.length
          ? state.accountRoles
          : [
              { value: "admin", label: "系统管理员" },
              { value: "librarian", label: "流通馆员" },
            ];
      const isSelf = isEdit && auth.admin && Number(auth.admin.id) === Number(account.id);
      const body = `
        <div class="form-grid">
          <div class="field full"><label>用户名<span class="req">*</span></label>
            <input name="username" type="text" autocomplete="off" ${
              isEdit ? "disabled" : ""
            } value="${esc(account ? account.username : "")}" placeholder="3–32 位字母/数字/_ . @ -" />
            <span class="tip">${
              isEdit
                ? "用户名是登录标识，创建后不可修改"
                : "登录后台用的账号，不能与读者的借书证号或手机号重名"
            }</span>
          </div>
          <div class="field full"><label>姓名</label>
            <input name="display_name" type="text" value="${esc(account ? account.display_name : "")}" placeholder="留空则与用户名相同" />
          </div>
          <div class="field full"><label>角色<span class="req">*</span></label>
            <select name="role" ${isSelf ? "disabled" : ""}>
              ${roles
                .map(
                  (r) =>
                    `<option value="${esc(r.value)}"${
                      account && account.role === r.value ? " selected" : ""
                    }>${esc(r.label)}</option>`
                )
                .join("")}
            </select>
            <span class="tip">系统管理员：全权，含账号管理与操作日志；流通馆员：图书编目、读者档案、借还续借，不能管理账号也看不到日志</span>
            ${isSelf ? '<span class="tip is-warn">不能修改自己的角色，请让另一位系统管理员操作</span>' : ""}
          </div>
          ${
            isEdit
              ? `<div class="field full"><label class="check"><input type="checkbox" name="is_active" ${
                  account.is_active ? "checked" : ""
                } ${isSelf ? "disabled" : ""} /> 启用该账号（取消勾选即停用，停用后立即无法登录）</label></div>`
              : `<div class="field full"><label>初始密码<span class="req">*</span></label>
                  <input name="password" type="password" autocomplete="new-password" />
                  <span class="tip">至少 6 位，创建后请通知本人登录并尽快自行修改</span>
                </div>`
          }
        </div>`;
      openModal({
        title: isEdit ? `编辑账号 · ${account.username}` : "新建后台账号",
        body,
        confirmText: isEdit ? "保存修改" : "创建账号",
        onSubmit: async (form) => {
          if (isEdit) {
            const patch = { display_name: form.display_name.value };
            if (form.role && !form.role.disabled) patch.role = form.role.value;
            if (form.is_active) patch.is_active = form.is_active.checked;
            await api.put(`/api/accounts/${account.id}`, patch);
            toast("账号已更新", `${account.username} 的信息已保存`, "ok");
          } else {
            const username = form.username.value.trim();
            const password = form.password.value;
            if (!username) {
              throw new ApiError("请输入用户名", 400, "validation_error", "username");
            }
            if (!password || password.length < 6) {
              throw new ApiError("初始密码至少 6 位", 400, "validation_error", "password");
            }
            const created = await api.post("/api/accounts", {
              username,
              password,
              display_name: form.display_name.value,
              role: form.role.value,
            });
            toast("账号已创建", `${created.username}（${created.role_label}）已可登录后台`, "ok");
          }
          await loadAccounts();
        },
      });
    };

    if (!isEdit) {
      api
        .get("/api/accounts")
        .then((payload) => {
          state.accountRoles = payload.roles || state.accountRoles;
          build(null);
        })
        .catch(showError);
      return;
    }
    api
      .get(`/api/accounts/${accountId}`)
      .then(build)
      .catch(showError);
  }

  function openAccountPassword(accountId, username) {
    openModal({
      title: `重置密码 · ${username}`,
      body: `
        <div class="form-grid">
          <div class="field full"><label>新密码<span class="req">*</span></label>
            <input name="password" type="password" autocomplete="new-password" />
            <span class="tip">至少 6 位。重置不会让该账号已登录的会话立即失效，若怀疑账号被盗用请同时停用它。</span>
          </div>
        </div>`,
      confirmText: "确认重置",
      onSubmit: async (form) => {
        const password = form.password.value;
        if (!password || password.length < 6) {
          throw new ApiError("新密码至少 6 位", 400, "validation_error", "password");
        }
        await api.post(`/api/accounts/${accountId}/password`, { password });
        toast("密码已重置", `${username} 可使用新密码登录`, "ok");
      },
    });
  }

  function confirmDeleteAccount(accountId, username) {
    openModal({
      title: "删除后台账号",
      body: `<p>确定删除账号「<b>${esc(username)}</b>」吗？该账号将立即无法登录；<b>历史操作日志会保留</b>，仍可追溯它做过的操作。</p>`,
      confirmText: "删除账号",
      danger: true,
      onSubmit: async () => {
        await api.del(`/api/accounts/${accountId}`);
        toast("账号已删除", `${username} 已无法登录后台`, "ok");
        await loadAccounts();
      },
    });
  }

  /* ---------------- 操作日志（仅系统管理员） ---------------- */

  async function loadLogs() {
    const s = state.logs;
    await withViewLoading("logs", async () => {
      const payload = await api.get("/api/logs", {
        page: s.page,
        page_size: s.pageSize,
        keyword: s.keyword,
        actor_type: s.actorType,
        action: s.action,
        status: s.status,
      });
      renderLogs(payload);
    });
  }

  function renderLogs(payload) {
    const table = $("#logTable");
    const actionSelect = $("#logAction");
    // 动作下拉只需填一次（选项来自后端动作表，避免前后端两处维护）
    if (actionSelect && actionSelect.options.length <= 1 && (payload.actions || []).length) {
      actionSelect.innerHTML =
        `<option value="">全部动作</option>` +
        payload.actions
          .map((a) => `<option value="${esc(a.value)}">${esc(a.label)}</option>`)
          .join("");
      actionSelect.value = state.logs.action;
    }
    const hint = $("#logHint");
    if (hint) hint.textContent = payload.total ? `共 ${payload.total} 条记录` : "";

    const items = payload.items || [];
    if (!items.length) {
      table.innerHTML = emptyState(6, "没有匹配的操作记录", "清除筛选");
      const clear = table.querySelector("[data-clear]");
      if (clear) clear.addEventListener("click", resetLogs);
      return;
    }

    table.innerHTML = `<thead><tr>
        <th>时间</th><th>操作者</th><th>动作</th><th>对象</th><th>结果</th><th class="wrap">摘要</th>
      </tr></thead>
      <tbody>${items
        .map((l) => {
          const denied = l.status === "denied";
          const target = l.entity_label || l.entity || "—";
          const targetText = l.entity_id ? `${target} #${esc(l.entity_id)}` : target;
          const actorSub = [l.actor_type_label, l.actor_role && l.actor_role !== l.actor_type ? l.actor_role : ""]
            .filter(Boolean)
            .join(" · ");
          return `<tr${denied ? ' class="is-inactive"' : ""}>
            <td class="sub">${esc(fmtDateTime(l.created_at))}</td>
            <td>${esc(l.actor_display || "—")}<div class="sub">${esc(actorSub)}</div></td>
            <td>${esc(l.action_label || l.action)}<div class="sub mono">${esc(l.action)}</div></td>
            <td>${esc(targetText)}</td>
            <td>${
              denied ? '<span class="badge danger">已拦截</span>' : '<span class="badge ok">已执行</span>'
            }</td>
            <td class="wrap">${esc(l.summary)}</td>
          </tr>`;
        })
        .join("")}</tbody>`;

    renderPager($("#logPager"), payload, (patch) => updateFilters(patch));
  }

  function resetLogs() {
    state.logs = {
      page: 1,
      pageSize: state.logs.pageSize,
      keyword: "",
      actorType: "",
      action: "",
      status: "",
    };
    syncToolbar("logs");
    syncUrl();
    loadLogs().catch(showError);
  }

  /* ---------------- 事件绑定 ---------------- */

  function bindEvents() {
    $("#nav").addEventListener("click", (e) => {
      const btn = e.target.closest(".nav-item");
      if (btn) navigateTo(btn.dataset.view);
    });

    document.addEventListener("click", (e) => {
      const jump = e.target.closest("[data-jump]");
      if (jump) {
        if (jump.dataset.status) state.loans.status = jump.dataset.status;
        state.loans.page = 1;
        navigateTo(jump.dataset.jump);
      }
    });

    // 表头排序：表格内容每次重渲染都会换掉，所以事件委托绑在容器上（只绑一次）
    [["#bookTable", "books"], ["#memberTable", "members"], ["#loanTable", "loans"]].forEach(([sel, view]) => {
      $(sel).addEventListener("click", (e) => {
        const th = e.target.closest("th[data-sort]");
        if (th) toggleSort(view, th.dataset.sort);
      });
    });

    // 图书
    $("#bookKeyword").addEventListener("input", debounce((e) => {
      updateFilters({ keyword: e.target.value.trim(), page: 1 });
    }, 300));
    $("#bookCategory").addEventListener("change", (e) => updateFilters({ category: e.target.value, page: 1 }));
    $("#bookAvailableOnly").addEventListener("change", (e) => updateFilters({ availableOnly: e.target.checked, page: 1 }));
    $("#bookReset").addEventListener("click", resetBooks);
    $("#bookCreate").addEventListener("click", () => openBookForm(null));
    $("#bookExport").addEventListener("click", () => exportBooks());

    // 读者
    $("#memberKeyword").addEventListener("input", debounce((e) => {
      updateFilters({ keyword: e.target.value.trim(), page: 1 });
    }, 300));
    $("#memberStatus").addEventListener("change", (e) => updateFilters({ status: e.target.value, page: 1 }));
    $("#memberReset").addEventListener("click", resetMembers);
    $("#memberCreate").addEventListener("click", () => openMemberForm(null));
    $("#memberExport").addEventListener("click", () => exportMembers());

    // 借阅
    $("#loanTabs").addEventListener("click", (e) => {
      const tab = e.target.closest(".tab");
      if (!tab) return;
      updateFilters({ status: tab.dataset.status, page: 1 });
    });
    $("#loanKeyword").addEventListener("input", debounce((e) => {
      updateFilters({ keyword: e.target.value.trim(), page: 1 });
    }, 300));
    $("#loanReset").addEventListener("click", resetLoans);
    $("#loanBorrow").addEventListener("click", () => openBorrowForm(null));
    $("#loanExport").addEventListener("click", () => exportLoans());
    $("#loanReturnBatch").addEventListener("click", openBatchReturn);

    $("#modalClose").addEventListener("click", closeModal);
    $("#modalLayer").addEventListener("click", (e) => {
      if (e.target === $("#modalLayer")) closeModal();
    });
    document.addEventListener("keydown", (e) => {
      if ($("#modalLayer").hidden) return;
      if (e.key === "Escape") closeModal();
      else if (e.key === "Tab") trapTab(e);
    });

    $("#logoutBtn").addEventListener("click", () => {
      openModal({
        title: "退出登录",
        body: `<p>确定要退出后台吗？未保存的表单内容将会丢失。</p>`,
        confirmText: "退出登录",
        danger: true,
        onSubmit: doLogout,
      });
    });
    $("#changePwdBtn").addEventListener("click", openChangePassword);

    // 账号管理（仅系统管理员可见；事件委托绑在表格上，重渲染不丢监听）
    const accountKw = $("#accountKeyword");
    if (accountKw) {
      accountKw.addEventListener(
        "input",
        debounce((e) => {
          state.accounts.keyword = e.target.value.trim();
          syncUrl();
          loadAccounts().catch(showError);
        }, 300)
      );
    }
    const accountResetBtn = $("#accountReset");
    if (accountResetBtn) accountResetBtn.addEventListener("click", resetAccounts);
    const accountCreateBtn = $("#accountCreate");
    if (accountCreateBtn) accountCreateBtn.addEventListener("click", () => openAccountForm(null));
    const accountTable = $("#accountTable");
    if (accountTable) {
      accountTable.addEventListener("click", (e) => {
        const edit = e.target.closest("[data-edit]");
        if (edit) {
          openAccountForm(Number(edit.dataset.edit));
          return;
        }
        const reset = e.target.closest("[data-resetpw]");
        if (reset) {
          openAccountPassword(Number(reset.dataset.resetpw), reset.dataset.username);
          return;
        }
        const del = e.target.closest("[data-del]");
        if (del && !del.disabled) {
          confirmDeleteAccount(Number(del.dataset.del), del.dataset.username);
        }
      });
    }

    // 操作日志
    const logKw = $("#logKeyword");
    if (logKw) {
      logKw.addEventListener(
        "input",
        debounce((e) => {
          state.logs.keyword = e.target.value.trim();
          state.logs.page = 1;
          syncUrl();
          loadLogs().catch(showError);
        }, 300)
      );
    }
    [["#logActorType", "actorType"], ["#logAction", "action"], ["#logStatus", "status"]].forEach(
      ([selector, key]) => {
        const node = $(selector);
        if (!node) return;
        node.addEventListener("change", (e) => {
          state.logs[key] = e.target.value;
          state.logs.page = 1;
          syncUrl();
          loadLogs().catch(showError);
        });
      }
    );
    const logResetBtn = $("#logReset");
    if (logResetBtn) logResetBtn.addEventListener("click", resetLogs);
    const bannerBtn = $("#securityBannerBtn");
    if (bannerBtn) bannerBtn.addEventListener("click", openChangePassword);
    const sessionBtn = $("#sessionBannerBtn");
    if (sessionBtn) sessionBtn.addEventListener("click", onSessionBannerClick);
  }

  /* ---------------- 启动 ---------------- */

  document.addEventListener("DOMContentLoaded", async () => {
    bindEvents();
    try {
      await loadSession();
    } catch (err) {
      return; // 401 已触发跳转
    }
    // hash 是「当前视图 + 筛选条件」的唯一真相：首次加载与浏览器前进/后退共用同一条路径
    window.addEventListener("hashchange", onHashChange);
    onHashChange();
  });
})();
