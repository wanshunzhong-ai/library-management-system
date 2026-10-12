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
    applications: { title: "办证审核", sub: "审核在线办证申请并发放读者证", permission: "member:write" },
    loans: { title: "借阅管理", sub: "办理借书、还书与续借" },
    fines: { title: "滞纳金管理", sub: "查询缴纳流水、馆员代收与减免", permission: "loan:write" },
    reservations: { title: "预约管理", sub: "查看预约队列、通知到书与取消", permission: "loan:write" },
    compensations: { title: "赔偿处理", sub: "丢书 / 损书赔付登记、结案与库存联动", permission: "loan:write" },
    stocktaking: { title: "盘点", sub: "开盘点单、录入实盘数、差异报告与调账", permission: "book:write" },
    acquisitions: { title: "采编验收", sub: "采购到货登记、ISBN 去重加册或新建书目", permission: "book:write" },
    announcements: { title: "公告管理", sub: "维护门户公告与开馆时间", permission: "announcement:write" },
    engagement: { title: "读者互动", sub: "审核读者评价、荐购登记与资料变更申请", permission: "member:write" },
    imports: { title: "批量导入", sub: "用 CSV / Excel 批量建档图书与读者，先预览后导入", permission: "book:write" },
    accounts: { title: "账号管理", sub: "后台账号、角色与启停用", permission: "account:manage" },
    approvals: { title: "审批中心", sub: "审核超阈值的罚金减免申请", permission: "approval:approve" },
    logs: { title: "操作日志", sub: "谁在什么时候改了什么", permission: "log:read" },
    system: { title: "系统维护", sub: "后台任务、邮件渠道与数据备份", permission: "account:manage" },
  };

  const state = {
    view: "dashboard",
    books: { page: 1, pageSize: 10, keyword: "", category: "", availableOnly: false, sort: "", order: "" },
    members: { page: 1, pageSize: 10, keyword: "", status: "", sort: "", order: "" },
    applications: { page: 1, pageSize: 10, keyword: "", status: "pending" },
    loans: { page: 1, pageSize: 10, keyword: "", status: "all", sort: "", order: "" },
    fines: { page: 1, pageSize: 20, keyword: "", kind: "" },
    reservations: { page: 1, pageSize: 10, keyword: "", status: "waiting" },
    compensations: { page: 1, pageSize: 10, keyword: "", status: "pending", kind: "" },
    stocktaking: { page: 1, pageSize: 10, keyword: "", status: "", location: "" },
    acquisitions: { page: 1, pageSize: 10, keyword: "", status: "pending" },
    imports: { kind: "books", xlsx: "", fileName: "" },
    announcements: { page: 1, pageSize: 10, keyword: "", status: "", kind: "" },
    engagement: { kind: "reviews", page: 1, pageSize: 10, keyword: "", status: "" },
    accounts: { keyword: "" },
    approvals: { page: 1, pageSize: 10, keyword: "", status: "pending" },
    system: { pane: "jobs", jobs: null, mail: null, backups: [] },
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
    applications: {
      table: "#applicationTable",
      headers: ["申请人", "联系方式", "证件", "类型", "提交时间", "状态", "操作"],
    },
    loans: {
      table: "#loanTable",
      headers: ["选择", "图书", "读者", "借出日期", "应还日期", "归还日期", "续借", "状态", "罚金", "操作"],
    },
    reservations: {
      table: "#reservationTable",
      headers: ["图书", "读者", "预约时间", "状态", "到书时间", "操作"],
    },
    fines: {
      table: "#fineTable",
      headers: ["时间", "读者", "图书", "类型", "金额", "方式", "经办人", "备注"],
    },
    compensations: {
      table: "#compensationTable",
      headers: ["图书", "读者", "类型", "应赔", "已收", "待结", "状态", "登记时间", "操作"],
    },
    stocktaking: {
      table: "#stocktakingTable",
      headers: ["单号", "盘点范围", "应盘", "已盘", "进度", "盘盈 / 盘亏", "状态", "开始时间", "操作"],
    },
    acquisitions: {
      table: "#acquisitionTable",
      headers: ["书名 / ISBN", "作者 / 出版社", "分类 / 位置", "册数 / 单价", "金额", "供应商", "状态", "操作"],
    },
    announcements: {
      table: "#announcementTable",
      headers: ["标题 / 正文", "类型", "重要程度", "生效窗口", "状态", "更新时间", "操作"],
    },
    engagement: {
      table: "#engagementTable",
      headers: ["对象", "读者", "内容", "说明", "提交时间", "状态", "操作"],
    },
    accounts: {
      table: "#accountTable",
      headers: ["用户名", "姓名", "角色", "状态", "最近登录", "创建时间", "操作"],
    },
    approvals: {
      table: "#approvalTable",
      headers: ["审批内容", "读者", "金额", "申请人 / 时间", "状态", "审批人 / 意见", "操作"],
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
    } else if (view === "applications") {
      const s = state.applications;
      if (s.keyword) params.set("kw", s.keyword);
      if (s.page > 1) params.set("page", String(s.page));
      if (s.pageSize !== DEFAULT_PAGE_SIZE) params.set("size", String(s.pageSize));
      if (s.status && s.status !== "pending") params.set("st", s.status);
    } else if (view === "announcements") {
      const s = state.announcements;
      if (s.keyword) params.set("kw", s.keyword);
      if (s.page > 1) params.set("page", String(s.page));
      if (s.pageSize !== DEFAULT_PAGE_SIZE) params.set("size", String(s.pageSize));
      if (s.status) params.set("st", s.status);
      if (s.kind) params.set("kind", s.kind);
    } else if (view === "engagement") {
      const s = state.engagement;
      params.set("kind", s.kind);
      if (s.keyword) params.set("kw", s.keyword);
      if (s.page > 1) params.set("page", String(s.page));
      if (s.pageSize !== DEFAULT_PAGE_SIZE) params.set("size", String(s.pageSize));
      if (s.status) params.set("st", s.status);
    } else if (view === "loans") {
      write(state.loans);
      if (state.loans.status && state.loans.status !== "all") params.set("st", state.loans.status);
    } else if (view === "fines") {
      const s = state.fines;
      if (s.keyword) params.set("kw", s.keyword);
      if (s.page > 1) params.set("page", String(s.page));
      if (s.pageSize !== 20) params.set("size", String(s.pageSize));
      if (s.kind) params.set("kind", s.kind);
    } else if (view === "compensations") {
      const s = state.compensations;
      if (s.keyword) params.set("kw", s.keyword);
      if (s.page > 1) params.set("page", String(s.page));
      if (s.pageSize !== DEFAULT_PAGE_SIZE) params.set("size", String(s.pageSize));
      if (s.status && s.status !== "pending") params.set("st", s.status);
      if (s.kind) params.set("kind", s.kind);
    } else if (view === "stocktaking") {
      const s = state.stocktaking;
      if (s.keyword) params.set("kw", s.keyword);
      if (s.page > 1) params.set("page", String(s.page));
      if (s.pageSize !== DEFAULT_PAGE_SIZE) params.set("size", String(s.pageSize));
      if (s.status) params.set("st", s.status);
      if (s.location) params.set("loc", s.location);
    } else if (view === "acquisitions") {
      const s = state.acquisitions;
      if (s.keyword) params.set("kw", s.keyword);
      if (s.page > 1) params.set("page", String(s.page));
      if (s.pageSize !== DEFAULT_PAGE_SIZE) params.set("size", String(s.pageSize));
      if (s.status && s.status !== "pending") params.set("st", s.status);
    } else if (view === "imports") {
      if (state.imports.kind !== "books") params.set("kind", state.imports.kind);
    } else if (view === "reservations") {
      const s = state.reservations;
      if (s.keyword) params.set("kw", s.keyword);
      if (s.page > 1) params.set("page", String(s.page));
      if (s.pageSize !== DEFAULT_PAGE_SIZE) params.set("size", String(s.pageSize));
      if (s.status && s.status !== "waiting") params.set("st", s.status);
    } else if (view === "accounts") {
      if (state.accounts.keyword) params.set("kw", state.accounts.keyword);
    } else if (view === "approvals") {
      const s = state.approvals;
      if (s.keyword) params.set("kw", s.keyword);
      if (s.page > 1) params.set("page", String(s.page));
      if (s.pageSize !== DEFAULT_PAGE_SIZE) params.set("size", String(s.pageSize));
      if (s.status && s.status !== "pending") params.set("st", s.status);
    } else if (view === "system") {
      if (state.system.pane !== "jobs") params.set("pane", state.system.pane);
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
    } else if (v === "applications") {
      const kw = $("#applicationKeyword");
      if (kw) kw.value = state.applications.keyword;
      document.querySelectorAll("#applicationTabs .tab").forEach((t) =>
        t.classList.toggle("is-active", t.dataset.status === state.applications.status)
      );
    } else if (v === "announcements") {
      const kw = $("#announcementKeyword");
      if (kw) kw.value = state.announcements.keyword;
      const kind = $("#announcementKind");
      if (kind) kind.value = state.announcements.kind;
      document.querySelectorAll("#announcementTabs .tab").forEach((t) =>
        t.classList.toggle("is-active", t.dataset.status === state.announcements.status)
      );
    } else if (v === "engagement") {
      const kw = $("#engagementKeyword");
      if (kw) kw.value = state.engagement.keyword;
      document.querySelectorAll("#engagementKindTabs .tab").forEach((t) =>
        t.classList.toggle("is-active", t.dataset.kind === state.engagement.kind)
      );
      renderEngagementStatusTabs();
    } else if (v === "loans") {
      $("#loanKeyword").value = state.loans.keyword;
      document.querySelectorAll("#loanTabs .tab").forEach((t) =>
        t.classList.toggle("is-active", t.dataset.status === state.loans.status)
      );
    } else if (v === "fines") {
      const kw = $("#fineKeyword");
      if (kw) kw.value = state.fines.keyword;
      document.querySelectorAll("#fineTabs .tab").forEach((t) =>
        t.classList.toggle("is-active", (t.dataset.kind || "") === state.fines.kind)
      );
    } else if (v === "compensations") {
      const kw = $("#compensationKeyword");
      if (kw) kw.value = state.compensations.keyword;
      const kind = $("#compensationKind");
      if (kind) kind.value = state.compensations.kind;
      document.querySelectorAll("#compensationTabs .tab").forEach((t) =>
        t.classList.toggle("is-active", t.dataset.status === state.compensations.status)
      );
    } else if (v === "stocktaking") {
      const kw = $("#stocktakingKeyword");
      if (kw) kw.value = state.stocktaking.keyword;
      const loc = $("#stocktakingLocation");
      if (loc) loc.value = state.stocktaking.location;
      document.querySelectorAll("#stocktakingTabs .tab").forEach((t) =>
        t.classList.toggle("is-active", (t.dataset.status || "") === state.stocktaking.status)
      );
    } else if (v === "acquisitions") {
      const kw = $("#acquisitionKeyword");
      if (kw) kw.value = state.acquisitions.keyword;
      document.querySelectorAll("#acquisitionTabs .tab").forEach((t) =>
        t.classList.toggle("is-active", t.dataset.status === state.acquisitions.status)
      );
    } else if (v === "imports") {
      document.querySelectorAll("#importKindTabs .tab").forEach((t) =>
        t.classList.toggle("is-active", t.dataset.kind === state.imports.kind)
      );
    } else if (v === "reservations") {
      const box = $("#reservationKeyword");
      if (box) box.value = state.reservations.keyword;
      document.querySelectorAll("#reservationTabs .tab").forEach((t) =>
        t.classList.toggle("is-active", t.dataset.status === state.reservations.status)
      );
    } else if (v === "accounts") {
      const box = $("#accountKeyword");
      if (box) box.value = state.accounts.keyword;
    } else if (v === "approvals") {
      const kw = $("#approvalKeyword");
      if (kw) kw.value = state.approvals.keyword;
      document.querySelectorAll("#approvalTabs .tab").forEach((t) =>
        t.classList.toggle("is-active", (t.dataset.status || "") === state.approvals.status)
      );
    } else if (v === "system") {
      const pane = state.system.pane || "jobs";
      document.querySelectorAll("#systemTabs .tab").forEach((t) =>
        t.classList.toggle("is-active", t.dataset.pane === pane)
      );
      [["jobs", "#systemPaneJobs"], ["mail", "#systemPaneMail"], ["backups", "#systemPaneBackups"]].forEach(
        ([key, sel]) => {
          const node = $(sel);
          if (node) node.hidden = key !== pane;
        }
      );
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
    } else if (view === "applications") {
      Object.assign(state.applications, {
        keyword: kw,
        status: params.get("st") || "pending",
        page,
        pageSize: size,
      });
    } else if (view === "announcements") {
      Object.assign(state.announcements, {
        keyword: kw,
        status: params.get("st") || "",
        kind: params.get("kind") || "",
        page,
        pageSize: size,
      });
    } else if (view === "engagement") {
      Object.assign(state.engagement, {
        kind: params.get("kind") || "reviews",
        keyword: kw,
        status: params.get("st") || "",
        page,
        pageSize: size,
      });
    } else if (view === "accounts") {
      Object.assign(state.accounts, { keyword: kw });
    } else if (view === "approvals") {
      Object.assign(state.approvals, {
        keyword: kw,
        status: params.get("st") || "pending",
        page,
        pageSize: size,
      });
    } else if (view === "system") {
      state.system.pane = params.get("pane") || "jobs";
    } else if (view === "fines") {
      Object.assign(state.fines, {
        keyword: kw,
        kind: params.get("kind") || "",
        page,
        pageSize: positiveInt(params.get("size"), 20),
      });
    } else if (view === "compensations") {
      Object.assign(state.compensations, {
        keyword: kw,
        status: params.get("st") || "pending",
        kind: params.get("kind") || "",
        page,
        pageSize: size,
      });
    } else if (view === "stocktaking") {
      Object.assign(state.stocktaking, {
        keyword: kw,
        status: params.get("st") || "",
        location: params.get("loc") || "",
        page,
        pageSize: size,
      });
    } else if (view === "acquisitions") {
      Object.assign(state.acquisitions, {
        keyword: kw,
        status: params.get("st") || "pending",
        page,
        pageSize: size,
      });
    } else if (view === "imports") {
      Object.assign(state.imports, { kind: params.get("kind") || "books" });
    } else if (view === "reservations") {
      Object.assign(state.reservations, {
        keyword: kw,
        status: params.get("st") || "waiting",
        page,
        pageSize: size,
      });
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
                  ${can("member:write") ? `<button class="btn sm" data-edit="${m.id}">编辑</button>
                  <button class="btn sm" data-files="${m.id}" data-name="${esc(m.name)}">附件</button>` : ""}
                  <button class="btn sm" data-history="${m.id}" data-name="${esc(m.name)}" data-card="${esc(m.card_no)}">借阅记录</button>
                  ${can("member:write") ? `<button class="btn sm" data-resetpw="${m.id}" data-name="${esc(m.name)}" data-card="${esc(m.card_no)}">重置密码</button>
                  <button class="btn sm" data-notify="${m.id}" data-name="${esc(m.name)}" data-card="${esc(m.card_no)}">发通知</button>
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
      table.querySelectorAll("[data-notify]").forEach((btn) =>
        btn.addEventListener("click", () =>
          openMemberNotify(Number(btn.dataset.notify), btn.dataset.name, btn.dataset.card)
        )
      );
      table.querySelectorAll("[data-files]").forEach((btn) =>
        btn.addEventListener("click", () =>
          openMemberFilesDialog(Number(btn.dataset.files), btn.dataset.name)
        )
      );
    });
  }

  /** 馆员主动给读者发一条站内通知（催还、到馆提醒、活动告知……）。 */
  function openMemberNotify(id, name, card) {
    openModal({
      title: `发送站内通知 · ${name || "读者 #" + id}`,
      body: `
        <div class="form-grid">
          <div class="field full"><label>通知标题<span class="req">*</span></label>
            <input name="title" maxlength="80" placeholder="例如：请尽快归还逾期图书" /></div>
          <div class="field full"><label>通知正文</label>
            <textarea name="body" rows="3" maxlength="500" placeholder="补充说明，会显示在读者个人中心的消息列表里（选填）"></textarea></div>
          <div class="field full"><label>读者点开后跳到</label>
            <select name="link_view">
              <option value="">不跳转</option>
              <option value="loans">我的借阅</option>
              <option value="reservations">我的预约</option>
              <option value="fines">滞纳金</option>
              <option value="bookshelf">我的书架</option>
              <option value="reviews">图书评价</option>
              <option value="requests">荐购登记</option>
              <option value="profile">个人资料</option>
            </select></div>
        </div>
        <div class="form-note">通知会出现在 ${esc(name || "该读者")}（${esc(card || "—")}）的个人中心「消息中心」里；系统没有短信 / 邮件推送，请勿用于紧急事项。</div>`,
      confirmText: "发送通知",
      onSubmit: async (form) => {
        const data = readForm(form);
        if (!data.title) throw new Error("请填写通知标题");
        await api.post(`/api/members/${id}/notify`, {
          title: data.title,
          body: data.body || "",
          link_view: data.link_view || "",
        });
        toast("已发送", `${name || "读者 #" + id} 会在个人中心看到这条通知`, "ok");
      },
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
                  <button class="btn sm ghost" data-receipt="${r.id}"
                          data-book="${esc(r.book_title)}" data-member="${esc(r.member_name)}" data-card="${esc(r.member_card)}"
                          data-barcode="${esc(r.book_barcode || "")}" data-isbn="${esc(r.book_isbn || "")}"
                          data-borrowed="${esc(r.borrowed_at || "")}" data-due-full="${esc(r.due_at || "")}">凭证</button>
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
      table.querySelectorAll("[data-receipt]").forEach((btn) =>
        btn.addEventListener("click", () =>
          printLoanReceipt({
            book_title: btn.dataset.book,
            member_name: btn.dataset.member,
            member_card: btn.dataset.card,
            book_barcode: btn.dataset.barcode,
            book_isbn: btn.dataset.isbn,
            borrowed_at: btn.dataset.borrowed,
            due_at: btn.dataset.dueFull,
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

  /* ---------------- 预约管理视图 ---------------- */

  function reservationBadge(r) {
    if (r.status === "waiting") {
      const pos = Number(r.queue_position) || 0;
      return `<span class="badge info">排队中${pos ? ` · 第 ${pos} 位` : ""}</span>`;
    }
    if (r.status === "ready") return `<span class="badge success">已到书</span>`;
    if (r.status === "fulfilled") return `<span class="badge neutral">已借出</span>`;
    if (r.status === "cancelled") return `<span class="badge neutral">已取消</span>`;
    if (r.status === "expired") return `<span class="badge warn">已过期</span>`;
    return `<span class="badge neutral">${esc(r.status_label || r.status || "")}</span>`;
  }

  async function loadReservations() {
    const s = state.reservations;
    await withViewLoading("reservations", async () => {
      const payload = await api.get("/api/reservations", {
        keyword: s.keyword,
        status: s.status,
        page: s.page,
        page_size: s.pageSize,
      });
      const table = $("#reservationTable");
      if (!payload.items.length) {
        const hasFilter = Boolean(s.keyword || (s.status && s.status !== "waiting"));
        table.innerHTML = emptyState(6, "没有匹配的预约记录", hasFilter ? "清除筛选条件" : "");
        const clear = table.querySelector("[data-clear]");
        if (clear) clear.addEventListener("click", resetReservations);
      } else {
        const writable = can("loan:write");
        table.innerHTML = `
          <thead><tr>
            <th>图书</th><th>读者</th><th>预约时间</th><th>状态</th><th>到书时间</th><th>操作</th>
          </tr></thead>
          <tbody>${payload.items
            .map((r) => {
              const open = r.status === "waiting" || r.status === "ready";
              let actions = '<span class="sub">—</span>';
              if (writable) {
                const parts = [];
                if (r.can_ready) {
                  parts.push(`<button class="btn sm" data-ready="${r.id}"
                          data-book="${esc(r.book_title)}" data-member="${esc(r.member_name)}">通知到书</button>`);
                }
                parts.push(`<button class="btn sm ghost" data-cancel="${r.id}" ${open ? "" : "disabled"}
                          data-book="${esc(r.book_title)}" data-member="${esc(r.member_name)}">取消</button>`);
                actions = `<div class="actions">${parts.join("")}</div>`;
              }
              return `<tr>
                <td class="wrap title-cell"><strong>${esc(r.book_title)}</strong><div class="sub">${esc(r.book_barcode || "—")} · 可借 ${Number(r.available_copies) || 0}/${Number(r.total_copies) || 0}</div></td>
                <td>${esc(r.member_name)}<div class="sub">${esc(r.member_card)}</div></td>
                <td class="sub">${esc(fmtDateTime(r.created_at))}</td>
                <td>${reservationBadge(r)}</td>
                <td class="sub">${r.ready_at ? esc(fmtDateTime(r.ready_at)) : "—"}</td>
                <td>${actions}</td>
              </tr>`;
            })
            .join("")}</tbody>`;
      }

      renderPager($("#reservationPager"), payload, (patch) => updateFilters(patch));

      const hint = $("#reservationHint");
      if (hint) {
        const waiting = Number(payload.waiting_total) || 0;
        hint.textContent = waiting ? `当前排队中 ${waiting} 条，还书后将自动按序通知` : "暂无排队中的预约";
      }

      table.querySelectorAll("[data-ready]").forEach((btn) =>
        btn.addEventListener("click", () =>
          doReservationReady(Number(btn.dataset.ready), {
            book: btn.dataset.book,
            member: btn.dataset.member,
          })
        )
      );
      table.querySelectorAll("[data-cancel]").forEach((btn) =>
        btn.addEventListener("click", () =>
          doReservationCancel(Number(btn.dataset.cancel), {
            book: btn.dataset.book,
            member: btn.dataset.member,
          })
        )
      );
    });
  }

  function resetReservations() {
    state.reservations = { page: 1, pageSize: state.reservations.pageSize, keyword: "", status: "waiting" };
    const box = $("#reservationKeyword");
    if (box) box.value = "";
    document.querySelectorAll("#reservationTabs .tab").forEach((t) =>
      t.classList.toggle("is-active", t.dataset.status === "waiting")
    );
    syncUrl();
    loadReservations().catch(showError);
  }

  /** 通知到书：把排队中的预约置为「已到书」。 */
  function doReservationReady(id, info) {
    const ctx = info || {};
    const label = ctx.book ? `《${esc(ctx.book)}》` : `预约 #${id}`;
    const reader = ctx.member ? `读者 ${esc(ctx.member)}` : "";
    openModal({
      title: "通知到书",
      body: `<p>确认通知 ${reader || "该读者"} 到馆借阅 ${label}？</p>
             <div class="form-note">通知后该预约进入「已到书」，读者在个人中心即可看到「请到馆借阅」提示。</div>`,
      confirmText: "确认通知",
      onSubmit: async () => {
        await api.post(`/api/reservations/${id}/ready`, {});
        toast("已通知到书", `${label}${reader ? ` · ${reader}` : ""} 已转入「已到书」`, "ok");
        await loadReservations();
      },
    });
  }

  /** 取消预约：把未结束的预约置为「已取消」。 */
  function doReservationCancel(id, info) {
    const ctx = info || {};
    const label = ctx.book ? `《${esc(ctx.book)}》` : `预约 #${id}`;
    const reader = ctx.member ? `读者 ${esc(ctx.member)}` : "该读者";
    openModal({
      title: "取消预约",
      body: `<p>确定取消 ${reader} 对 ${label} 的预约？</p>
             <div class="form-note">取消后该读者不再保留排队位次；如需重新借阅可再次发起预约。</div>`,
      confirmText: "取消预约",
      danger: true,
      onSubmit: async () => {
        await api.post(`/api/reservations/${id}/cancel`, {});
        toast("已取消预约", `${label} · ${reader}`, "ok");
        await loadReservations();
      },
    });
  }

  /* ---------------- 办证审核视图 ---------------- */

  // 上一次列表返回的表单可选项（读者类型 / 证件类型由后端给出，前端不硬编码）
  let applicationMeta = { member_types: [], id_types: [] };

  function applicationBadge(a) {
    if (a.status === "pending") return `<span class="badge warn">待审核</span>`;
    if (a.status === "approved") return `<span class="badge success">已通过</span>`;
    if (a.status === "rejected") return `<span class="badge neutral">已驳回</span>`;
    return `<span class="badge neutral">${esc(a.status_label || a.status || "")}</span>`;
  }

  async function loadApplications() {
    const s = state.applications;
    await withViewLoading("applications", async () => {
      const payload = await api.get("/api/applications", {
        keyword: s.keyword,
        status: s.status,
        page: s.page,
        page_size: s.pageSize,
      });
      applicationMeta = {
        member_types: payload.member_types || [],
        id_types: payload.id_types || [],
      };

      const table = $("#applicationTable");
      if (!payload.items.length) {
        const hasFilter = Boolean(s.keyword || (s.status && s.status !== "pending"));
        table.innerHTML = emptyState(7, "没有匹配的办证申请", hasFilter ? "清除筛选条件" : "");
        const clear = table.querySelector("[data-clear]");
        if (clear) clear.addEventListener("click", resetApplications);
      } else {
        const writable = can("member:write");
        table.innerHTML = `
          <thead><tr>
            <th>申请人</th><th>联系方式</th><th>证件</th><th>类型</th><th>提交时间</th><th>状态</th><th>操作</th>
          </tr></thead>
          <tbody>${payload.items
            .map((a) => {
              let extra = "";
              if (a.status === "rejected" && a.reject_reason) {
                extra = `<div class="sub">驳回：${esc(a.reject_reason)}</div>`;
              } else if (a.status === "approved") {
                extra = `<div class="sub mono">证号 ${esc(a.card_no || "—")}${
                  a.reviewed_by ? ` · ${esc(a.reviewed_by)}` : ""
                }</div>`;
              }
              let actions = '<span class="sub">已处理</span>';
              if (writable && a.status === "pending") {
                actions = `<div class="actions">
                  <button class="btn sm" data-approve="${a.id}" data-name="${esc(a.name)}">通过并发证</button>
                  <button class="btn sm ghost" data-reject="${a.id}" data-name="${esc(a.name)}">驳回</button>
                </div>`;
              }
              return `<tr>
                <td class="wrap title-cell"><strong>${esc(a.name)}</strong><div class="sub mono">${esc(a.code)}</div></td>
                <td>${esc(a.phone_masked)}<div class="sub">${esc(a.email || "—")}</div></td>
                <td>${esc(a.id_type)}<div class="sub mono">${esc(a.id_tail_display)}</div></td>
                <td>${esc(a.member_type)}</td>
                <td class="sub">${esc(fmtDateTime(a.created_at))}</td>
                <td>${applicationBadge(a)}${extra}</td>
                <td>${actions}</td>
              </tr>`;
            })
            .join("")}</tbody>`;
      }

      renderPager($("#applicationPager"), payload, (patch) => updateFilters(patch));

      const hint = $("#applicationHint");
      if (hint) {
        const c = payload.status_counts || {};
        hint.textContent = `待审核 ${c.pending || 0} · 已通过 ${c.approved || 0} · 已驳回 ${c.rejected || 0}`;
      }

      table.querySelectorAll("[data-approve]").forEach((btn) =>
        btn.addEventListener("click", () =>
          doApproveApplication(Number(btn.dataset.approve), btn.dataset.name || "")
        )
      );
      table.querySelectorAll("[data-reject]").forEach((btn) =>
        btn.addEventListener("click", () =>
          doRejectApplication(Number(btn.dataset.reject), btn.dataset.name || "")
        )
      );
    });
  }

  function resetApplications() {
    state.applications = { page: 1, pageSize: state.applications.pageSize, keyword: "", status: "pending" };
    const box = $("#applicationKeyword");
    if (box) box.value = "";
    document.querySelectorAll("#applicationTabs .tab").forEach((t) =>
      t.classList.toggle("is-active", t.dataset.status === "pending")
    );
    syncUrl();
    loadApplications().catch(showError);
  }

  /** 审核通过：可改证号（留空自动生成）与读者类型，成功后把初始密码交给馆员转告。 */
  function doApproveApplication(id, name) {
    const types = applicationMeta.member_types.length ? applicationMeta.member_types : ["普通"];
    openModal({
      title: `审核通过 · ${name || "申请 #" + id}`,
      body: `
        <div class="form-grid">
          <div class="field"><label>姓名<span class="req">*</span></label><input name="name" value="${esc(name)}" /></div>
          <div class="field"><label>读者类型<span class="req">*</span></label>
            <select name="member_type">${types
              .map((t) => `<option ${t === "社会" ? "selected" : ""}>${esc(t)}</option>`)
              .join("")}</select>
          </div>
          <div class="field"><label>读者证号</label><input name="card_no" placeholder="留空自动生成 R00001 形式" /></div>
          <div class="field"><label>可借上限</label><input name="max_borrow" placeholder="留空使用默认值" /></div>
        </div>
        <div class="form-note">通过后立即开通门户账号，请把生成的初始密码转告读者，并提醒其登录后尽快修改。</div>`,
      confirmText: "通过并发证",
      onSubmit: async (form) => {
        const data = readForm(form);
        if (!data.max_borrow) delete data.max_borrow;
        const result = await api.post(`/api/applications/${id}/approve`, data);
        toast(
          "已发证",
          `${result.member.name}（${result.card_no}）初始密码 ${result.initial_password}，请转告读者`,
          "ok"
        );
        await loadApplications();
      },
    });
  }

  function doRejectApplication(id, name) {
    openModal({
      title: `驳回申请 · ${name || "#" + id}`,
      body: `
        <div class="field full"><label>驳回原因<span class="req">*</span></label>
          <textarea name="reason" rows="3" placeholder="例如：证件信息不完整，请携带身份证到馆核验"></textarea>
        </div>
        <div class="form-note">原因会展示给申请人（凭申请编号 + 手机号查询时可见），请写清楚需要补充什么。</div>`,
      confirmText: "确认驳回",
      danger: true,
      onSubmit: async (form) => {
        await api.post(`/api/applications/${id}/reject`, readForm(form));
        toast("已驳回", `${name || "申请 #" + id} 的办证申请已驳回`, "ok");
        await loadApplications();
      },
    });
  }

  /* ---------------- 公告管理视图 ---------------- */

  const ANNOUNCEMENT_KINDS = [
    { value: "notice", label: "公告" },
    { value: "hours", label: "开馆时间" },
  ];
  const ANNOUNCEMENT_LEVELS = [
    { value: "info", label: "普通" },
    { value: "important", label: "重要" },
  ];
  const ANNOUNCEMENT_STATUSES = [
    { value: "published", label: "已发布" },
    { value: "draft", label: "草稿" },
    { value: "archived", label: "已下线" },
  ];

  /** 把库里的 YYYY-MM-DD HH:MM:SS 转成 datetime-local 需要的 YYYY-MM-DDTHH:MM */
  const toLocalInput = (value) =>
    value ? String(value).replace(" ", "T").slice(0, 16) : "";

  function announcementBadge(a) {
    if (a.status === "published") {
      return a.effective
        ? `<span class="badge success">已发布</span>`
        : `<span class="badge warn">未生效</span>`;
    }
    if (a.status === "draft") return `<span class="badge neutral">草稿</span>`;
    if (a.status === "archived") return `<span class="badge neutral">已下线</span>`;
    return `<span class="badge neutral">${esc(a.status_label || a.status || "")}</span>`;
  }

  async function loadAnnouncements() {
    const s = state.announcements;
    await withViewLoading("announcements", async () => {
      const payload = await api.get("/api/announcements", {
        keyword: s.keyword,
        status: s.status,
        kind: s.kind,
        page: s.page,
        page_size: s.pageSize,
      });
      const table = $("#announcementTable");
      if (!payload.items.length) {
        const hasFilter = Boolean(s.keyword || s.status || s.kind);
        table.innerHTML = emptyState(7, "还没有公告", hasFilter ? "清除筛选条件" : "");
        const clear = table.querySelector("[data-clear]");
        if (clear) clear.addEventListener("click", resetAnnouncements);
      } else {
        const writable = can("announcement:write");
        table.innerHTML = `
          <thead><tr>
            <th>标题 / 正文</th><th>类型</th><th>重要程度</th><th>生效窗口</th><th>状态</th><th>更新时间</th><th>操作</th>
          </tr></thead>
          <tbody>${payload.items
            .map((a) => {
              const pin = a.pinned ? `<span class="badge info">置顶</span>` : "";
              let actions = '<span class="sub">—</span>';
              if (writable) {
                const next = a.status === "published" ? "archived" : "published";
                actions = `<div class="actions">
                  <button class="btn sm ghost" data-edit="${a.id}">编辑</button>
                  <button class="btn sm ghost" data-toggle="${a.id}" data-status="${next}">
                    ${a.status === "published" ? "下线" : "发布"}</button>
                  <button class="btn sm danger" data-del="${a.id}" data-title="${esc(a.title)}">删除</button>
                </div>`;
              }
              return `<tr>
                <td class="wrap title-cell"><strong>${esc(a.title)}</strong> ${pin}
                  <div class="sub">${esc(a.body || "（无正文）")}</div></td>
                <td><span class="badge neutral">${esc(a.kind_label)}</span></td>
                <td>${a.level === "important" ? `<span class="badge warn">重要</span>` : `<span class="sub">普通</span>`}</td>
                <td class="sub">${esc(a.window_label || "长期有效")}</td>
                <td>${announcementBadge(a)}</td>
                <td class="sub">${esc(fmtDateTime(a.updated_at || a.created_at))}</td>
                <td>${actions}</td>
              </tr>`;
            })
            .join("")}</tbody>`;
      }

      renderPager($("#announcementPager"), payload, (patch) => updateFilters(patch));

      table.querySelectorAll("[data-edit]").forEach((btn) =>
        btn.addEventListener("click", () => openAnnouncementForm(Number(btn.dataset.edit)))
      );
      table.querySelectorAll("[data-toggle]").forEach((btn) =>
        btn.addEventListener("click", () =>
          doAnnouncementStatus(Number(btn.dataset.toggle), btn.dataset.status)
        )
      );
      table.querySelectorAll("[data-del]").forEach((btn) =>
        btn.addEventListener("click", () =>
          confirmDeleteAnnouncement(Number(btn.dataset.del), btn.dataset.title || "")
        )
      );
    });
  }

  function resetAnnouncements() {
    state.announcements = { page: 1, pageSize: state.announcements.pageSize, keyword: "", status: "", kind: "" };
    const kw = $("#announcementKeyword");
    if (kw) kw.value = "";
    const kind = $("#announcementKind");
    if (kind) kind.value = "";
    document.querySelectorAll("#announcementTabs .tab").forEach((t) =>
      t.classList.toggle("is-active", t.dataset.status === "")
    );
    syncUrl();
    loadAnnouncements().catch(showError);
  }

  /* ---------------- 读者互动视图（评价 / 荐购 / 资料变更） ---------------- */

  // 三类互动共用一个视图：切「类别」时状态页签与接口一起换，避免导航里塞三项
  const ENGAGEMENT_KINDS = {
    reviews: {
      endpoint: "/api/engagement/reviews",
      defaultStatus: "",
      statuses: [
        { value: "", label: "全部" },
        { value: "published", label: "已发布" },
        { value: "hidden", label: "已隐藏" },
      ],
      emptyText: "还没有读者评价",
    },
    "book-requests": {
      endpoint: "/api/engagement/book-requests",
      defaultStatus: "pending",
      statuses: [
        { value: "pending", label: "待处理" },
        { value: "accepted", label: "已采纳" },
        { value: "purchased", label: "已采购" },
        { value: "rejected", label: "未采纳" },
        { value: "", label: "全部" },
      ],
      emptyText: "没有匹配的荐购登记",
    },
    "profile-requests": {
      endpoint: "/api/engagement/profile-requests",
      defaultStatus: "pending",
      statuses: [
        { value: "pending", label: "待审核" },
        { value: "approved", label: "已通过" },
        { value: "rejected", label: "已驳回" },
        { value: "", label: "全部" },
      ],
      emptyText: "没有匹配的资料变更申请",
    },
  };

  function engagementBadge(status) {
    if (status === "pending") return '<span class="badge warn">待处理</span>';
    if (status === "published" || status === "approved" || status === "accepted" || status === "purchased") {
      return '<span class="badge ok">已通过</span>';
    }
    if (status === "hidden" || status === "rejected") return '<span class="badge neutral">已驳回</span>';
    return `<span class="badge neutral">${esc(status || "")}</span>`;
  }

  function engagementStars(rating) {
    const n = Number(rating) || 0;
    return `<span class="stars" title="${n} 星">${[1, 2, 3, 4, 5]
      .map((i) => `<i class="${i <= n ? "on" : ""}">★</i>`)
      .join("")}</span>`;
  }

  function renderEngagementStatusTabs() {
    const box = $("#engagementStatusTabs");
    if (!box) return;
    const kind = ENGAGEMENT_KINDS[state.engagement.kind] || ENGAGEMENT_KINDS.reviews;
    box.innerHTML = kind.statuses
      .map(
        (s) =>
          `<button class="tab${s.value === state.engagement.status ? " is-active" : ""}" data-status="${s.value}" type="button">${s.label}</button>`
      )
      .join("");
  }

  async function loadEngagement() {
    const s = state.engagement;
    const kind = ENGAGEMENT_KINDS[s.kind] || ENGAGEMENT_KINDS.reviews;
    await withViewLoading("engagement", async () => {
      const payload = await api.get(kind.endpoint, {
        keyword: s.keyword,
        status: s.status,
        page: s.page,
        page_size: s.pageSize,
      });
      const table = $("#engagementTable");
      const rows = payload.items || [];
      if (!rows.length) {
        const hasFilter = Boolean(s.keyword || s.status);
        table.innerHTML = emptyState(7, kind.emptyText, hasFilter ? "清除筛选条件" : "");
        const clear = table.querySelector("[data-clear]");
        if (clear) clear.addEventListener("click", resetEngagement);
      } else if (s.kind === "reviews") {
        renderReviewRows(table, rows);
      } else if (s.kind === "book-requests") {
        renderBookRequestRows(table, rows);
      } else {
        renderProfileRequestRows(table, rows);
      }
      renderPager($("#engagementPager"), payload, (patch) => updateFilters(patch));

      const hint = $("#engagementHint");
      if (hint) {
        const c = payload.status_counts || {};
        hint.textContent = Object.keys(c)
          .filter((k) => k !== "all")
          .map((k) => `${(kind.statuses.find((x) => x.value === k) || {}).label || k} ${c[k]}`)
          .join(" · ");
      }
    });
    loadEngagementSummary().catch(() => {});
  }

  function renderReviewRows(table, rows) {
    table.innerHTML = `
      <thead><tr><th>图书</th><th>读者</th><th>评分</th><th>短评</th><th>提交时间</th><th>状态</th><th>操作</th></tr></thead>
      <tbody>${rows
        .map((r) => {
          const action =
            r.status === "published"
              ? `<button class="btn sm ghost" data-review-hide="${r.id}" data-title="${esc(r.book_title)}">隐藏</button>`
              : `<button class="btn sm" data-review-show="${r.id}" data-title="${esc(r.book_title)}">发布</button>`;
          return `<tr>
            <td class="title-cell"><strong>《${esc(r.book_title)}》</strong><div class="sub">${esc(r.book_author || "佚名")}</div></td>
            <td>${esc(r.member_name || "—")}<div class="sub mono">${esc(r.member_card || "")}</div></td>
            <td>${engagementStars(r.rating)}<div class="sub">${Number(r.rating) || 0} 星</div></td>
            <td class="wrap">${r.content ? esc(r.content) : '<span class="sub">（未填写短评）</span>'}</td>
            <td class="sub">${esc(fmtDateTime(r.updated_at || r.created_at))}</td>
            <td>${engagementBadge(r.status)}<div class="sub">${esc(r.status_label || "")}</div></td>
            <td><div class="actions">${action}</div></td>
          </tr>`;
        })
        .join("")}</tbody>`;
    table.querySelectorAll("[data-review-hide]").forEach((btn) =>
      btn.addEventListener("click", () => setReviewStatus(Number(btn.dataset.reviewHide), "hidden", btn.dataset.title))
    );
    table.querySelectorAll("[data-review-show]").forEach((btn) =>
      btn.addEventListener("click", () => setReviewStatus(Number(btn.dataset.reviewShow), "published", btn.dataset.title))
    );
  }

  function renderBookRequestRows(table, rows) {
    table.innerHTML = `
      <thead><tr><th>荐购图书</th><th>读者</th><th>书目信息</th><th>理由 / 答复</th><th>提交时间</th><th>状态</th><th>操作</th></tr></thead>
      <tbody>${rows
        .map((r) => {
          const extra =
            r.status === "rejected" && r.reply
              ? `<div class="sub">答复：${esc(r.reply)}</div>`
              : r.reply
                ? `<div class="sub">答复：${esc(r.reply)}</div>`
                : "";
          const actions = r.pending
            ? `<div class="actions">
                <button class="btn sm" data-req-accept="${r.id}" data-title="${esc(r.title)}">采纳</button>
                <button class="btn sm" data-req-purchase="${r.id}" data-title="${esc(r.title)}">已采购</button>
                <button class="btn sm ghost" data-req-reject="${r.id}" data-title="${esc(r.title)}">未采纳</button>
              </div>`
            : '<span class="sub">已处理</span>';
          return `<tr>
            <td class="title-cell"><strong>《${esc(r.title)}》</strong></td>
            <td>${esc(r.member_name || "—")}<div class="sub mono">${esc(r.member_card || "")}</div></td>
            <td class="wrap">${esc([r.author, r.publisher, r.isbn].filter(Boolean).join(" · ") || "—")}</td>
            <td class="wrap">${r.reason ? esc(r.reason) : '<span class="sub">（未填写理由）</span>'}${extra}</td>
            <td class="sub">${esc(fmtDateTime(r.created_at))}${r.reviewed_by ? `<div>处理：${esc(r.reviewed_by)}</div>` : ""}</td>
            <td>${engagementBadge(r.status)}<div class="sub">${esc(r.status_label || "")}</div></td>
            <td>${actions}</td>
          </tr>`;
        })
        .join("")}</tbody>`;
    [["data-req-accept", "accepted"], ["data-req-purchase", "purchased"], ["data-req-reject", "rejected"]].forEach(
      ([attr, status]) => {
        table.querySelectorAll(`[${attr}]`).forEach((btn) =>
          btn.addEventListener("click", () => reviewBookRequest(Number(btn.getAttribute(attr)), status, btn.dataset.title))
        );
      }
    );
  }

  function renderProfileRequestRows(table, rows) {
    table.innerHTML = `
      <thead><tr><th>读者</th><th>变更项</th><th>变更内容</th><th>说明 / 答复</th><th>提交时间</th><th>状态</th><th>操作</th></tr></thead>
      <tbody>${rows
        .map((r) => {
          const actions = r.pending
            ? `<div class="actions">
                <button class="btn sm" data-pr-approve="${r.id}" data-name="${esc(r.member_name)}">通过</button>
                <button class="btn sm ghost" data-pr-reject="${r.id}" data-name="${esc(r.member_name)}">驳回</button>
              </div>`
            : '<span class="sub">已处理</span>';
          return `<tr>
            <td>${esc(r.member_name || "—")}<div class="sub mono">${esc(r.member_card || "")}</div></td>
            <td><span class="badge neutral">${esc(r.field_label || r.field)}</span></td>
            <td class="wrap"><span class="sub">${esc(r.old_value || "—")}</span> → <strong>${esc(r.new_value)}</strong></td>
            <td class="wrap">${r.reason ? esc(r.reason) : '<span class="sub">（未填写说明）</span>'}${
              r.reply ? `<div class="sub">答复：${esc(r.reply)}</div>` : ""
            }</td>
            <td class="sub">${esc(fmtDateTime(r.created_at))}${r.reviewed_by ? `<div>处理：${esc(r.reviewed_by)}</div>` : ""}</td>
            <td>${engagementBadge(r.status)}<div class="sub">${esc(r.status_label || "")}</div></td>
            <td>${actions}</td>
          </tr>`;
        })
        .join("")}</tbody>`;
    table.querySelectorAll("[data-pr-approve]").forEach((btn) =>
      btn.addEventListener("click", () => reviewProfileRequest(Number(btn.dataset.prApprove), true, btn.dataset.name))
    );
    table.querySelectorAll("[data-pr-reject]").forEach((btn) =>
      btn.addEventListener("click", () => reviewProfileRequest(Number(btn.dataset.prReject), false, btn.dataset.name))
    );
  }

  /** 待处理汇总：底部三张小卡 + 导航角标（让馆员一进门就看见有活儿）。 */
  async function loadEngagementSummary() {
    const summary = await api.get("/api/engagement/summary");
    const box = $("#engagementPending");
    if (box) {
      const cards = [
        { label: "待处理荐购", value: summary.book_requests, hint: "读者希望馆里采购的书" },
        { label: "待审核资料变更", value: summary.profile_requests, hint: "姓名 / 读者类型修改申请" },
        { label: "已发布评价", value: summary.reviews, hint: "当前在门户上展示的短评" },
      ];
      box.innerHTML = cards
        .map(
          (c) => `<div class="pending-item">
            <div class="p-label">${esc(c.label)}</div>
            <div class="p-value">${Number(c.value) || 0}</div>
            <div class="p-hint">${esc(c.hint)}</div>
          </div>`
        )
        .join("");
    }
    renderEngagementBadge(summary);
    return summary;
  }

  function renderEngagementBadge(summary) {
    const badge = $("#engagementBadge");
    if (!badge) return;
    const total = summary ? Number(summary.total) || 0 : 0;
    badge.hidden = total <= 0;
    badge.textContent = total > 99 ? "99+" : String(total);
  }

  function setReviewStatus(id, status, title) {
    const hiding = status === "hidden";
    openModal({
      title: `${hiding ? "隐藏" : "发布"}评价 · 《${title || "#" + id}》`,
      body: `<div class="form-note">${
        hiding
          ? "隐藏后这条评价不再出现在门户与读者端，读者自己也看不到；可随时重新发布。"
          : "发布后会立即出现在门户的图书详情页，读者姓名只显示姓氏。"
      }</div>`,
      confirmText: hiding ? "确认隐藏" : "确认发布",
      danger: hiding,
      onSubmit: async () => {
        await api.post(`/api/engagement/reviews/${id}/status`, { status });
        toast(hiding ? "已隐藏" : "已发布", `《${title || "评价 #" + id}》的评价状态已更新`, "ok");
        await loadEngagement();
      },
    });
  }

  function reviewBookRequest(id, status, title) {
    const label = { accepted: "采纳", purchased: "已采购", rejected: "未采纳" }[status] || status;
    openModal({
      title: `${label} · 《${title || "#" + id}》`,
      body: `
        <div class="field full"><label>给读者的答复${status === "rejected" ? '<span class="req">*</span>' : ""}</label>
          <textarea name="reply" rows="3" placeholder="${
            status === "rejected"
              ? "未采纳时必须说明原因，读者会看到这条答复"
              : "例如：已列入采购清单，到馆后会在新书架上架"
          }"></textarea></div>
        <div class="form-note">结论与答复会以站内通知的形式发给荐购的读者。</div>`,
      confirmText: `确认${label}`,
      danger: status === "rejected",
      onSubmit: async (form) => {
        const data = readForm(form);
        await api.post(`/api/engagement/book-requests/${id}/review`, { status: status, reply: data.reply || "" });
        toast("已处理", `《${title || "荐购 #" + id}》已标记为「${label}」`, "ok");
        await loadEngagement();
      },
    });
  }

  function reviewProfileRequest(id, approve, name) {
    const approving = !!approve;
    openModal({
      title: `${approving ? "通过" : "驳回"}资料变更 · ${name || "#" + id}`,
      body: `
        <div class="field full"><label>给读者的答复${approving ? "" : '<span class="req">*</span>'}</label>
          <textarea name="reply" rows="3" placeholder="${
            approving ? "选填，例如：已核对证件，资料已更新" : "驳回时必须说明原因，读者会看到这条答复"
          }"></textarea></div>
        <div class="form-note">${
          approving
            ? "通过后系统会立即回写读者资料，并通知读者结果。"
            : "驳回不会改动读者资料，原因会通过站内通知发给读者。"
        }</div>`,
      confirmText: approving ? "确认通过" : "确认驳回",
      danger: !approving,
      onSubmit: async (form) => {
        const data = readForm(form);
        await api.post(`/api/engagement/profile-requests/${id}/review`, { approve: approving, reply: data.reply || "" });
        toast(approving ? "已通过" : "已驳回", `${name || "申请 #" + id} 的资料变更申请已处理`, "ok");
        await loadEngagement();
      },
    });
  }

  function resetEngagement() {
    const kind = ENGAGEMENT_KINDS[state.engagement.kind] || ENGAGEMENT_KINDS.reviews;
    state.engagement = {
      kind: state.engagement.kind,
      page: 1,
      pageSize: state.engagement.pageSize,
      keyword: "",
      status: kind.defaultStatus,
    };
    const kw = $("#engagementKeyword");
    if (kw) kw.value = "";
    renderEngagementStatusTabs();
    syncUrl();
    loadEngagement().catch(showError);
  }

  function openAnnouncementForm(id) {
    const isEdit = !!id;
    const load = isEdit ? api.get(`/api/announcements/${id}`) : Promise.resolve(null);
    load
      .then((item) => {
        const a = item || {};
        openModal({
          title: isEdit ? `编辑公告 · ${a.title || "#" + id}` : "新建公告",
          body: `
            <div class="form-grid">
              <div class="field full"><label>标题<span class="req">*</span></label>
                <input name="title" value="${esc(a.title || "")}" placeholder="例如：国庆假期开馆安排" /></div>
              <div class="field"><label>类型<span class="req">*</span></label>
                <select name="kind">${ANNOUNCEMENT_KINDS.map(
                  (k) => `<option value="${k.value}" ${(a.kind || "notice") === k.value ? "selected" : ""}>${k.label}</option>`
                ).join("")}</select>
                <span class="tip">「开馆时间」会单独展示在门户「关于本馆」里</span></div>
              <div class="field"><label>重要程度</label>
                <select name="level">${ANNOUNCEMENT_LEVELS.map(
                  (l) => `<option value="${l.value}" ${(a.level || "info") === l.value ? "selected" : ""}>${l.label}</option>`
                ).join("")}</select></div>
              <div class="field"><label>状态<span class="req">*</span></label>
                <select name="status">${ANNOUNCEMENT_STATUSES.map(
                  (s) => `<option value="${s.value}" ${(a.status || "published") === s.value ? "selected" : ""}>${s.label}</option>`
                ).join("")}</select></div>
              <div class="field"><label>置顶</label>
                <select name="pinned">
                  <option value="0" ${a.pinned ? "" : "selected"}>否</option>
                  <option value="1" ${a.pinned ? "selected" : ""}>是</option>
                </select></div>
              <div class="field"><label>生效时间</label>
                <input name="publish_at" type="datetime-local" value="${esc(toLocalInput(a.publish_at))}" />
                <span class="tip">留空表示立即生效</span></div>
              <div class="field"><label>失效时间</label>
                <input name="expire_at" type="datetime-local" value="${esc(toLocalInput(a.expire_at))}" />
                <span class="tip">留空表示长期有效；只填日期则当天结束失效</span></div>
              <div class="field full"><label>正文</label>
                <textarea name="body" rows="4" placeholder="面向读者的说明文字">${esc(a.body || "")}</textarea></div>
            </div>`,
          confirmText: isEdit ? "保存修改" : "发布公告",
          onSubmit: async (form) => {
            const data = readForm(form);
            const result = isEdit
              ? await api.put(`/api/announcements/${id}`, data)
              : await api.post("/api/announcements", data);
            toast(
              isEdit ? "公告已更新" : "公告已创建",
              `《${result.title}》${result.effective ? "已生效" : "尚未生效"}`,
              "ok"
            );
            await loadAnnouncements();
          },
        });
      })
      .catch(showError);
  }

  function doAnnouncementStatus(id, status) {
    const verb = status === "published" ? "发布" : "下线";
    openModal({
      title: `${verb}公告`,
      body: `<p>确定${verb}这条公告吗？</p>
             <div class="form-note">${
               status === "published"
                 ? "发布后立即出现在门户首页（若设了生效时间则以时间为准）。"
                 : "下线后门户首页不再展示，历史记录保留在后台。"
             }</div>`,
      confirmText: `确认${verb}`,
      danger: status !== "published",
      onSubmit: async () => {
        await api.post(`/api/announcements/${id}/status`, { status });
        toast(`已${verb}`, "门户首页已同步", "ok");
        await loadAnnouncements();
      },
    });
  }

  function confirmDeleteAnnouncement(id, title) {
    openModal({
      title: "删除公告",
      body: `<p>确定删除公告 <b>《${esc(title)}》</b> 吗？</p>
             <div class="form-note">删除后不可恢复；如果只是暂时不想展示，建议改为「下线」。</div>`,
      confirmText: "确认删除",
      danger: true,
      onSubmit: async () => {
        await api.del(`/api/announcements/${id}`);
        toast("公告已删除", `《${esc(title)}》`, "ok");
        await loadAnnouncements();
      },
    });
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
          <label class="check"><input type="checkbox" name="print_receipt" value="1" /> 借出成功后打印借书凭证</label>
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
        const wantsPrint = form.querySelector('[name="print_receipt"]');
        await refreshAfterLoanChange();
        // 借完直接出凭证：柜面常见的「一边借一边给凭条」，省掉再去列表找这一步
        if (wantsPrint && wantsPrint.checked) printLoanReceipt(result);
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
    // 读者互动待处理角标：馆员一进门就能看见「有活儿没审」
    if (can("member:write")) loadEngagementSummary().catch(() => {});
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
    // 有审批权时顺手拉一次待审批角标（失败静默）
    refreshApprovalBadge();
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
    if (state.view === "fines") return loadFines();
    if (state.view === "compensations") return loadCompensations();
    if (state.view === "stocktaking") return loadStocktaking();
    if (state.view === "acquisitions") return loadAcquisitions();
    if (state.view === "imports") return loadImportsView();
    if (state.view === "applications") return loadApplications();
    if (state.view === "reservations") return loadReservations();
    if (state.view === "announcements") return loadAnnouncements();
    if (state.view === "engagement") return loadEngagement();
    if (state.view === "accounts") return loadAccounts();
    if (state.view === "approvals") return loadApprovals();
    if (state.view === "system") return loadSystem();
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

  /* ==================================================================
   * 第 11 批：流通馆员能力
   *   滞纳金管理 / 赔偿处理 / 盘点 / 采编验收 / 批量导入 /
   *   批量催还 / 打印凭证与催还单
   * ================================================================== */

  /** 顶部汇总小卡（滞纳金 / 赔偿两个视图共用）。 */
  function renderSumGrid(node, cards) {
    if (!node) return;
    node.innerHTML = cards
      .map(
        (c) => `<div class="sum-item${c.tone ? " is-" + c.tone : ""}">
          <span class="s-label">${esc(c.label)}</span>
          <b class="s-value">${esc(String(c.value))}</b>
          <span class="s-hint">${esc(c.hint || "")}</span>
        </div>`
      )
      .join("");
  }

  function tipNode(node, text, tone) {
    if (!node) return;
    node.textContent = text || "";
    node.hidden = !text;
    node.className = "import-tip" + (tone ? " is-" + tone : "");
  }

  /* ---------------- 通用：读者选择器 ---------------- */

  /** 两步式弹窗的第一步：搜读者并选中。选中后回调，由调用方接着开下一步。 */
  function openMemberPicker(title, onPick) {
    openModal({
      title: title || "选择读者",
      body: `
        <div class="field full">
          <label>搜索读者</label>
          <input name="q" type="search" autocomplete="off" placeholder="姓名 / 借书证号 / 手机号" />
          <span class="tip">输入至少 1 个字开始检索，点结果即选中</span>
        </div>
        <div class="pick-list" id="pickList"><div class="pick-empty">输入关键字开始搜索</div></div>`,
      confirmText: "关闭",
      onSubmit: async () => {},
      onMount: (form) => {
        const box = form.querySelector("#pickList");
        const input = form.querySelector('[name="q"]');
        const render = (items) => {
          if (!items.length) {
            box.innerHTML = `<div class="pick-empty">没有匹配的读者</div>`;
            return;
          }
          box.innerHTML = items
            .map(
              (m) => `<button type="button" class="pick-item" data-id="${m.id}"
                data-name="${esc(m.name)}" data-card="${esc(m.card_no)}" data-phone="${esc(m.phone || "")}">
                <b>${esc(m.name)}</b><span>${esc(m.card_no)} · 在借 ${Number(m.active_loans) || 0}/${Number(m.max_borrow) || 0}${
                Number(m.overdue_loans) ? " · 有逾期" : ""
              }</span></button>`
            )
            .join("");
          box.querySelectorAll(".pick-item").forEach((btn) =>
            btn.addEventListener("click", () => {
              closeModal();
              onPick({ id: Number(btn.dataset.id), name: btn.dataset.name, card: btn.dataset.card });
            })
          );
        };
        const run = debounce((kw) => {
          if (!kw.trim()) {
            box.innerHTML = `<div class="pick-empty">输入关键字开始搜索</div>`;
            return;
          }
          api
            .get("/api/members", { keyword: kw.trim(), page: 1, page_size: 8 })
            .then((p) => render(p.items || []))
            .catch(showError);
        }, 280);
        input.addEventListener("input", (e) => run(e.target.value));
      },
    });
  }

  /* ---------------- 滞纳金管理视图 ---------------- */

  async function loadFines() {
    const s = state.fines;
    await withViewLoading("fines", async () => {
      const payload = await api.get("/api/fines", {
        keyword: s.keyword,
        kind: s.kind,
        page: s.page,
        page_size: s.pageSize,
      });
      const totals = payload.totals || {};
      renderSumGrid($("#fineSummary"), [
        { label: "已收取合计", value: money(totals.paid), hint: "当前筛选范围", tone: "ok" },
        { label: "已减免合计", value: money(totals.waived), hint: "当前筛选范围", tone: "warn" },
        { label: "流水笔数", value: String(payload.total), hint: "含缴纳与减免" },
      ]);

      const table = $("#fineTable");
      if (!payload.items.length) {
        const hasFilter = Boolean(s.keyword || s.kind);
        table.innerHTML = emptyState(8, "没有匹配的缴纳流水", hasFilter ? "清除筛选条件" : "");
        const clear = table.querySelector("[data-clear]");
        if (clear) clear.addEventListener("click", resetFines);
      } else {
        table.innerHTML = `
          <thead><tr>
            <th>时间</th><th>读者</th><th>图书</th><th>类型</th>
            <th class="num">金额</th><th>方式</th><th>经办人</th><th>备注</th>
          </tr></thead>
          <tbody>${payload.items
            .map(
              (p) => `<tr>
                <td class="sub">${esc(fmtDateTime(p.created_at))}</td>
                <td>${esc(p.member_name || "—")}<div class="sub">${esc(p.member_card || "—")}</div></td>
                <td class="wrap title-cell"><strong>${esc(p.book_title || "—")}</strong>
                  <div class="sub">ISBN ${esc(p.book_isbn || "—")}</div></td>
                <td>${
                  p.kind === "waiver"
                    ? '<span class="badge warn">减免</span>'
                    : '<span class="badge success">缴纳</span>'
                }</td>
                <td class="num">${money(p.amount)}</td>
                <td class="sub">${esc(p.method_label || "—")}</td>
                <td class="sub">${esc(p.operator || "—")}</td>
                <td class="sub wrap">${esc(p.note || "—")}</td>
              </tr>`
            )
            .join("")}</tbody>`;
      }
      renderPager($("#finePager"), payload, (patch) => updateFilters(patch));
    });
  }

  function resetFines() {
    state.fines = { page: 1, pageSize: state.fines.pageSize, keyword: "", kind: "" };
    syncToolbar("fines");
    syncUrl();
    loadFines().catch(showError);
  }

  /** 代收 / 减免：先选读者 → 列出待缴明细 → 选一笔处理（减免强制填理由 + 二次确认）。 */
  function openFineCollectDialog() {
    openMemberPicker("代收滞纳金 · 选择读者", (member) => openFineOutstandingDialog(member));
  }

  function openFineOutstandingDialog(member) {
    api
      .get(`/api/fines/member/${member.id}`)
      .then((payload) => {
        const items = payload.items || [];
        if (!items.length) {
          toast("该读者没有待缴滞纳金", `${member.name}（${member.card}）当前无需缴费`, "ok");
          return;
        }
        openModal({
          title: `代收 / 减免 · ${member.name}`,
          body: `
            <div class="form-note">
              ${esc(member.name)}（${esc(member.card)}）当前待缴合计
              <b>${money(payload.total_outstanding)}</b>。选择一笔处理后提交。
            </div>
            <div class="field full"><label>待缴明细<span class="req">*</span></label>
              <select name="loan_id">
                ${items
                  .map(
                    (i) => `<option value="${i.loan_id}" data-outstanding="${i.outstanding}">
                      ${esc(`《${i.book_title}》逾期 ${i.overdue_days} 天 · 应缴 ${money(i.outstanding)}（${i.state_label}）`)}
                    </option>`
                  )
                  .join("")}
              </select>
            </div>
            <div class="form-grid">
              <div class="field"><label>处理方式<span class="req">*</span></label>
                <select name="mode">
                  <option value="payment">代收（收款）</option>
                  <option value="waiver">减免（免收）</option>
                </select>
              </div>
              <div class="field"><label>金额<span class="req">*</span></label>
                <input name="amount" inputmode="decimal" autocomplete="off" />
                <span class="tip">默认该笔全部应缴，可只收一部分</span>
              </div>
              <div class="field"><label>收款方式</label>
                <select name="method">
                  <option value="cash">现金</option>
                  <option value="online">线上</option>
                </select>
              </div>
            </div>
            <div class="field full"><label>备注 / 减免理由</label>
              <input name="note" autocomplete="off" placeholder="减免必填：写明依据（如「系统故障致逾期」「馆方责任」）" />
              <span class="tip">减免必须填写理由，便于事后核对</span>
            </div>`,
          confirmText: "提交",
          onMount: (form) => {
            const loanSel = form.loan_id;
            const amount = form.amount;
            const mode = form.mode;
            const method = form.method;
            const sync = () => {
              const opt = loanSel.options[loanSel.selectedIndex];
              amount.value = Number(opt && opt.dataset.outstanding) || 0;
            };
            loanSel.addEventListener("change", sync);
            mode.addEventListener("change", () => {
              const waiver = mode.value === "waiver";
              form.querySelector('[name="method"]').closest(".field").hidden = waiver;
              form.querySelector('[name="note"]').placeholder = waiver
                ? "减免必填：写明依据"
                : "选填";
            });
            sync();
          },
          onSubmit: async (form) => {
            const loanId = Number(form.loan_id.value);
            const mode = form.mode.value;
            const amount = Number(form.amount.value);
            const note = (form.note.value || "").trim();
            if (!loanId) throw new ApiError("请选择要处理的明细", 400, "validation_error", "loan_id");
            if (!(amount > 0)) throw new ApiError("金额必须大于 0", 400, "validation_error", "amount");
            if (mode === "waiver" && !note) {
              throw new ApiError("减免必须填写理由", 400, "validation_error", "note");
            }
            const body = { member_id: member.id, loan_id: loanId, amount, note };
            if (mode === "waiver") {
              // 减免是「不收这笔钱」的权力，金额与理由摆一起再确认一次
              const ok = await confirmFineWaiver(member, amount, note);
              if (!ok) throw new ApiError("已取消减免操作", 400, "cancelled");
              const result = await api.post("/api/fines/waive", body);
              if (result && result.status === "pending_approval") {
                // 金额超过审批阈值：减免没生效，转成一张待审批工单
                toast("已提交审批", result.message || "等待系统管理员审批后生效", "warn");
                refreshApprovalBadge();
              } else {
                toast("已减免", `${member.name} · ${money(amount)}`, "ok");
              }
            } else {
              body.method = form.method.value || "cash";
              await api.post("/api/fines/collect", body);
              toast("已收款", `${member.name} · ${money(amount)}`, "ok");
            }
            loadFines().catch(() => {});
          },
        });
      })
      .catch(showError);
  }

  /** 减免二次确认：把「谁、多少钱、什么理由」摆在一起，避免误点。 */
  function confirmFineWaiver(member, amount, reason) {
    return new Promise((resolve) => {
      let decided = false;
      openModal({
        title: "确认减免滞纳金",
        danger: true,
        body: `
          <div class="confirm-block">
            <p>即将为 <b>${esc(member.name)}</b>（${esc(member.card)}）减免滞纳金：</p>
            <p class="confirm-amount">${money(amount)}</p>
            <p class="confirm-reason">理由：${esc(reason)}</p>
            <p class="confirm-warn">减免后该笔金额不再计入应缴，操作会记入操作日志。</p>
          </div>`,
        confirmText: "确认减免",
        onSubmit: async () => {
          decided = true;
          resolve(true);
        },
      });
      const foot = $("#modalFoot");
      const cancel = foot.querySelector("[data-cancel]");
      if (cancel) {
        cancel.addEventListener("click", () => {
          if (!decided) resolve(false);
        });
      }
      const close = $("#modalClose");
      if (close) {
        close.addEventListener("click", () => {
          if (!decided) resolve(false);
        });
      }
    });
  }

  function exportFines() {
    const s = state.fines;
    return withExport($("#fineExport"), async () => {
      const rows = await fetchAllRows("/api/fines", { keyword: s.keyword, kind: s.kind }, 100);
      if (!rows.length) {
        toast("没有可导出的数据", "当前筛选条件下没有缴纳流水", "warn");
        return;
      }
      downloadCsv(
        `滞纳金流水-${fileStamp()}.csv`,
        ["时间", "读者", "借书证号", "图书", "ISBN", "类型", "金额", "方式", "经办人", "备注"],
        rows.map((p) => [
          fmtDateTime(p.created_at), p.member_name, p.member_card, p.book_title, p.book_isbn,
          p.kind_label, Number(p.amount).toFixed(2), p.method_label, p.operator, p.note,
        ])
      );
      toast("已导出滞纳金流水", `共 ${rows.length} 条记录`, "ok");
    });
  }

  /* ---------------- 赔偿处理视图 ---------------- */

  function compensationBadge(c) {
    if (c.status === "pending") return `<span class="badge warn">待结案</span>`;
    if (c.status === "settled") return `<span class="badge success">已结案</span>`;
    return `<span class="badge neutral">已注销</span>`;
  }

  async function loadCompensations() {
    const s = state.compensations;
    await withViewLoading("compensations", async () => {
      const payload = await api.get("/api/compensations", {
        keyword: s.keyword,
        status: s.status,
        kind: s.kind,
        page: s.page,
        page_size: s.pageSize,
      });
      const sum = payload.summaries || {};
      renderSumGrid($("#compensationSummary"), [
        { label: "待结案金额", value: money(sum.pending_amount), hint: "尚未收到赔付", tone: "warn" },
        { label: "已收赔付", value: money(sum.paid_total), hint: "当前筛选范围", tone: "ok" },
        { label: "单据数", value: String(sum.total), hint: "当前筛选范围" },
      ]);

      const table = $("#compensationTable");
      if (!payload.items.length) {
        const hasFilter = Boolean(s.keyword || s.kind || (s.status && s.status !== "pending"));
        table.innerHTML = emptyState(9, "没有匹配的赔偿单", hasFilter ? "清除筛选条件" : "");
        const clear = table.querySelector("[data-clear]");
        if (clear) clear.addEventListener("click", resetCompensations);
      } else {
        table.innerHTML = `
          <thead><tr>
            <th>图书</th><th>读者</th><th>类型</th><th class="num">应赔</th><th class="num">已收</th>
            <th class="num">待结</th><th>状态</th><th>登记时间</th><th>操作</th>
          </tr></thead>
          <tbody>${payload.items
            .map(
              (c) => `<tr>
                <td class="wrap title-cell"><strong>${esc(c.book_title)}</strong>
                  <div class="sub">${esc(c.book_barcode || "—")} · ISBN ${esc(c.book_isbn || "—")}</div></td>
                <td>${esc(c.member_name)}<div class="sub">${esc(c.member_card)}</div></td>
                <td>${c.kind === "lost" ? '<span class="badge danger">丢失</span>' : '<span class="badge warn">损毁</span>'}</td>
                <td class="num">${money(c.amount)}</td>
                <td class="num">${c.paid_amount > 0 ? money(c.paid_amount) : '<span class="sub">—</span>'}</td>
                <td class="num">${c.outstanding > 0 ? money(c.outstanding) : '<span class="sub">—</span>'}</td>
                <td>${compensationBadge(c)}${c.loan_open ? "" : '<div class="sub">借阅已归还</div>'}</td>
                <td class="sub">${esc(fmtDateTime(c.created_at))}</td>
                <td><div class="actions">
                  ${
                    c.can_settle
                      ? `<button class="btn sm primary" data-settle="${c.id}" data-title="${esc(c.book_title)}"
                             data-member="${esc(c.member_name)}" data-amount="${c.amount}" data-open="${c.loan_open ? "1" : ""}">结案</button>
                         <button class="btn sm danger" data-cancel="${c.id}" data-title="${esc(c.book_title)}">注销</button>`
                      : `<span class="sub">${esc(c.status_label)}</span>`
                  }
                </div></td>
              </tr>`
            )
            .join("")}</tbody>`;
      }
      renderPager($("#compensationPager"), payload, (patch) => updateFilters(patch));

      table.querySelectorAll("[data-settle]").forEach((btn) =>
        btn.addEventListener("click", () =>
          openCompensationSettle(Number(btn.dataset.settle), {
            title: btn.dataset.title,
            member: btn.dataset.member,
            amount: Number(btn.dataset.amount),
            loanOpen: btn.dataset.open === "1",
          })
        )
      );
      table.querySelectorAll("[data-cancel]").forEach((btn) =>
        btn.addEventListener("click", () =>
          cancelCompensation(Number(btn.dataset.cancel), btn.dataset.title)
        )
      );
    });
  }

  function resetCompensations() {
    state.compensations = {
      page: 1,
      pageSize: state.compensations.pageSize,
      keyword: "",
      status: "pending",
      kind: "",
    };
    syncToolbar("compensations");
    syncUrl();
    loadCompensations().catch(showError);
  }

  /** 登记赔偿：先选读者 → 列出其未归还（或已归还）借阅 → 选一笔登记赔付。 */
  function openCompensationForm() {
    openMemberPicker("登记赔偿 · 选择读者", (member) => {
      api
        .get("/api/loans", { keyword: member.card, page: 1, page_size: 50 })
        .then((payload) => {
          const open = (payload.items || []).filter((l) => Number(l.member_id) === member.id);
          if (!open.length) {
            toast("没有可登记的借阅", `${member.name} 名下没有借阅记录`, "warn");
            return;
          }
          openCompensationFormFor(member, open);
        })
        .catch(showError);
    });
  }

  function openCompensationFormFor(member, loans) {
    openModal({
      title: `登记赔偿 · ${member.name}`,
      body: `
        <div class="form-note">丢书 / 损书赔付登记后进入「待结案」，结案时才决定是否核销库存与减免逾期罚金。</div>
        <div class="field full"><label>借阅记录<span class="req">*</span></label>
          <select name="loan_id">
            ${loans
              .map(
                (l) => `<option value="${l.id}" data-returned="${l.returned_at ? "1" : ""}">
                  ${esc(`《${l.book_title}》· ${l.book_barcode || "无条码"} · 借于 ${fmtDate(l.borrowed_at)}${l.returned_at ? " · 已归还" : " · 未归还"}`)}
                </option>`
              )
              .join("")}
          </select>
          <span class="tip">「图书丢失」只能选未归还的记录；已归还的破损请选「图书损毁」</span>
        </div>
        <div class="form-grid">
          <div class="field"><label>赔偿类型<span class="req">*</span></label>
            <select name="kind">
              <option value="lost">图书丢失</option>
              <option value="damaged">图书损毁</option>
            </select>
          </div>
          <div class="field"><label>赔偿金额<span class="req">*</span></label>
            <input name="amount" inputmode="decimal" autocomplete="off" placeholder="按馆内赔偿标准填写" />
          </div>
        </div>
        <div class="field full"><label>备注</label><input name="note" autocomplete="off" placeholder="选填，如书价依据、破损情况" /></div>`,
      confirmText: "登记",
      onMount: (form) => {
        const loanSel = form.loan_id;
        const kind = form.kind;
        const sync = () => {
          const opt = loanSel.options[loanSel.selectedIndex];
          if (opt && opt.dataset.returned === "1" && kind.value === "lost") {
            kind.value = "damaged";
          }
        };
        loanSel.addEventListener("change", sync);
        sync();
      },
      onSubmit: async (form) => {
        const loanId = Number(form.loan_id.value);
        const amount = Number(form.amount.value);
        if (!loanId) throw new ApiError("请选择借阅记录", 400, "validation_error", "loan_id");
        if (!(amount > 0)) throw new ApiError("赔偿金额必须大于 0", 400, "validation_error", "amount");
        await api.post("/api/compensations", {
          loan_id: loanId,
          kind: form.kind.value,
          amount,
          note: form.note.value,
        });
        toast("已登记赔偿", `《${form.loan_id.options[form.loan_id.selectedIndex].text.trim()}》`, "ok");
        loadCompensations().catch(() => {});
      },
    });
  }

  function openCompensationSettle(compId, info) {
    openModal({
      title: "赔偿结案",
      body: `
        <div class="form-note">《${esc(info.title)}》· ${esc(info.member)} · 应赔 <b>${money(info.amount)}</b></div>
        <div class="field full"><label>实收金额<span class="req">*</span></label>
          <input name="paid_amount" inputmode="decimal" autocomplete="off" value="${info.amount.toFixed(2)}" />
          <span class="tip">留空或填全额表示已收到全部赔付；不得超过应赔金额</span>
        </div>
        ${
          info.loanOpen
            ? `<label class="check"><input type="checkbox" name="close_loan" value="1" checked />
                 按「图书丢失」结案：把该笔借阅置为已归还，并把总册数核减 1 册</label>
               <div class="form-note">勾选后系统不会回补可借库存——书已不在架上，只减总册数，避免出现「账上有书、架上没书」。</div>`
            : `<div class="form-note">该笔借阅已归还，结案只记账，不动库存。</div>`
        }
        <label class="check"><input type="checkbox" name="waive_fine" value="1" checked />
          同步减免该借阅的逾期滞纳金</label>
        <div class="field full"><label>备注</label><input name="note" autocomplete="off" placeholder="选填" /></div>`,
      confirmText: "确认结案",
      onMount: (form) => {
        const box = form.querySelector('[name="waive_fine"]');
        if (!info.loanOpen) box.checked = false;
      },
      onSubmit: async (form) => {
        const paid = Number(form.paid_amount.value);
        if (!(paid > 0)) throw new ApiError("实收金额必须大于 0", 400, "validation_error", "paid_amount");
        const closeLoan = form.querySelector('[name="close_loan"]');
        const waive = form.querySelector('[name="waive_fine"]');
        const result = await api.post(`/api/compensations/${compId}/settle`, {
          paid_amount: paid,
          close_loan: Boolean(closeLoan && closeLoan.checked),
          waive_fine: Boolean(waive && waive.checked),
          note: form.note.value,
        });
        const extra = [];
        if (result.closed_loan) extra.push("借阅已核销");
        if (Number(result.waived_fine) > 0) extra.push(`减免滞纳金 ${money(result.waived_fine)}`);
        toast("赔偿已结案", extra.length ? extra.join("，") : "已记账", "ok");
        loadCompensations().catch(() => {});
      },
    });
  }

  function cancelCompensation(compId, title) {
    openModal({
      title: "注销赔偿单",
      danger: true,
      body: `<div class="confirm-block">
        <p>即将注销《${esc(title)}》的赔偿单。</p>
        <p class="confirm-warn">注销适用于「登记错了」或「读者找回图书」，注销后不可恢复。</p>
      </div>
      <div class="field full"><label>备注</label><input name="note" autocomplete="off" placeholder="选填" /></div>`,
      confirmText: "确认注销",
      onSubmit: async (form) => {
        await api.post(`/api/compensations/${compId}/cancel`, { note: form.note.value });
        toast("已注销赔偿单", "记录保留在「已注销」页签下", "ok");
        loadCompensations().catch(() => {});
      },
    });
  }

  function exportCompensations() {
    const s = state.compensations;
    return withExport($("#compensationExport"), async () => {
      const rows = await fetchAllRows(
        "/api/compensations",
        { keyword: s.keyword, status: s.status, kind: s.kind },
        100
      );
      if (!rows.length) {
        toast("没有可导出的数据", "当前筛选条件下没有赔偿单", "warn");
        return;
      }
      downloadCsv(
        `赔偿单-${fileStamp()}.csv`,
        ["登记时间", "图书", "馆藏条码", "ISBN", "读者", "借书证号", "类型", "应赔金额", "已收金额", "待结金额", "状态", "经办人", "备注"],
        rows.map((c) => [
          fmtDateTime(c.created_at), c.book_title, c.book_barcode, c.book_isbn,
          c.member_name, c.member_card, c.kind_label,
          Number(c.amount).toFixed(2), Number(c.paid_amount).toFixed(2), Number(c.outstanding).toFixed(2),
          c.status_label, c.operator, c.note,
        ])
      );
      toast("已导出赔偿单", `共 ${rows.length} 条记录`, "ok");
    });
  }

  /* ---------------- 盘点视图 ---------------- */

  function stocktakingBadge(b) {
    if (b.status === "open") return `<span class="badge info">进行中</span>`;
    return `<span class="badge neutral">已关闭</span>`;
  }

  async function loadStocktaking() {
    const s = state.stocktaking;
    await withViewLoading("stocktaking", async () => {
      const payload = await api.get("/api/stocktaking", {
        keyword: s.keyword,
        status: s.status,
        location: s.location,
        page: s.page,
        page_size: s.pageSize,
      });
      const locSel = $("#stocktakingLocation");
      if (locSel) {
        const current = s.location;
        locSel.innerHTML =
          `<option value="">全部范围</option>` +
          (payload.locations || [])
            .map((l) => `<option value="${esc(l)}">${esc(l)}</option>`)
            .join("");
        locSel.value = current;
      }

      const table = $("#stocktakingTable");
      if (!payload.items.length) {
        const hasFilter = Boolean(s.keyword || s.status || s.location);
        table.innerHTML = emptyState(9, "还没有盘点单", hasFilter ? "清除筛选条件" : "");
        const clear = table.querySelector("[data-clear]");
        if (clear) clear.addEventListener("click", resetStocktaking);
      } else {
        table.innerHTML = `
          <thead><tr>
            <th>单号</th><th>盘点范围</th><th class="num">应盘</th><th class="num">已盘</th>
            <th>进度</th><th>盘盈 / 盘亏</th><th>状态</th><th>开始时间</th><th>操作</th>
          </tr></thead>
          <tbody>${payload.items
            .map(
              (b) => `<tr>
                <td><b>${esc(b.code)}</b><div class="sub">${esc(b.operator || "—")}</div></td>
                <td>${esc(b.location_label)}</td>
                <td class="num">${b.item_count}</td>
                <td class="num">${b.counted_count}</td>
                <td><div class="progress"><i style="width:${Math.min(100, b.progress)}%"></i></div>
                  <div class="sub">${b.progress}%${b.pending_count ? ` · 待盘 ${b.pending_count}` : ""}</div></td>
                <td>${
                  b.diff_count
                    ? `<span class="badge warn">${b.diff_count} 项有差异</span>`
                    : '<span class="sub">暂无差异</span>'
                }</td>
                <td>${stocktakingBadge(b)}</td>
                <td class="sub">${esc(fmtDateTime(b.created_at))}</td>
                <td><div class="actions">
                  <button class="btn sm primary" data-detail="${b.id}">${b.is_open ? "录入实盘" : "查看差异"}</button>
                  ${
                    b.is_open
                      ? `<button class="btn sm" data-close="${b.id}">关闭</button>
                         <button class="btn sm danger" data-del="${b.id}" data-code="${esc(b.code)}">删除</button>`
                      : `<button class="btn sm" data-reopen="${b.id}">重新打开</button>
                         <button class="btn sm" data-apply="${b.id}" data-code="${esc(b.code)}">按差异调账</button>`
                  }
                </div></td>
              </tr>`
            )
            .join("")}</tbody>`;
      }
      renderPager($("#stocktakingPager"), payload, (patch) => updateFilters(patch));

      table.querySelectorAll("[data-detail]").forEach((btn) =>
        btn.addEventListener("click", () => openStocktakingDetail(Number(btn.dataset.detail)))
      );
      table.querySelectorAll("[data-close]").forEach((btn) =>
        btn.addEventListener("click", () => closeStocktaking(Number(btn.dataset.close)))
      );
      table.querySelectorAll("[data-reopen]").forEach((btn) =>
        btn.addEventListener("click", () => reopenStocktaking(Number(btn.dataset.reopen)))
      );
      table.querySelectorAll("[data-apply]").forEach((btn) =>
        btn.addEventListener("click", () =>
          applyStocktaking(Number(btn.dataset.apply), btn.dataset.code)
        )
      );
      table.querySelectorAll("[data-del]").forEach((btn) =>
        btn.addEventListener("click", () =>
          deleteStocktaking(Number(btn.dataset.del), btn.dataset.code)
        )
      );
    });
  }

  function resetStocktaking() {
    state.stocktaking = {
      page: 1,
      pageSize: state.stocktaking.pageSize,
      keyword: "",
      status: "",
      location: "",
    };
    syncToolbar("stocktaking");
    syncUrl();
    loadStocktaking().catch(showError);
  }

  function openStocktakingCreate() {
    api
      .get("/api/stocktaking", { page: 1, page_size: 1 })
      .then((payload) => {
        const locations = payload.locations || [];
        openModal({
          title: "开启盘点单",
          body: `
            <div class="form-note">开单时会把该范围内的图书**快照**进明细，之后盘到哪本录哪本；同一范围同时只能有一张进行中的盘点单。</div>
            <div class="field full"><label>盘点范围</label>
              <select name="location">
                <option value="">全馆（所有图书）</option>
                ${locations.map((l) => `<option value="${esc(l)}">${esc(l)}</option>`).join("")}
              </select>
              <span class="tip">选全馆会一次拉进全部图书；按馆藏位置开单更适合分架清点</span>
            </div>
            <div class="field full"><label>备注</label><input name="note" autocomplete="off" placeholder="选填，如「三楼文学区季度盘点」" /></div>`,
          confirmText: "开启盘点",
          onSubmit: async (form) => {
            const batch = await api.post("/api/stocktaking", {
              location: form.location.value,
              note: form.note.value,
            });
            toast("盘点单已开启", `${batch.code} · 应盘 ${batch.item_count} 种`, "ok");
            await loadStocktaking();
            openStocktakingDetail(batch.id);
          },
        });
      })
      .catch(showError);
  }

  /** 盘点明细：录入实盘数（逐行） + 差异筛选 + 汇总。 */
  function openStocktakingDetail(batchId, stateFilter) {
    const filter = stateFilter || "";
    api
      .get(`/api/stocktaking/${batchId}`, filter ? { state: filter } : undefined)
      .then((batch) => {
        const s = batch.summary || {};
        const open = batch.status === "open";
        openModal({
          title: `盘点明细 · ${batch.code}`,
          body: `
            <div class="sum-grid compact">
              <div class="sum-item"><span class="s-label">应盘</span><b class="s-value">${s.items}</b><span class="s-hint">本次明细条数</span></div>
              <div class="sum-item is-ok"><span class="s-label">账实一致</span><b class="s-value">${s.matched}</b></div>
              <div class="sum-item is-warn"><span class="s-label">盘盈</span><b class="s-value">${s.up}</b></div>
              <div class="sum-item is-danger"><span class="s-label">盘亏</span><b class="s-value">${s.down}</b></div>
              <div class="sum-item"><span class="s-label">未盘</span><b class="s-value">${s.pending}</b></div>
              <div class="sum-item"><span class="s-label">差异合计</span><b class="s-value">${s.diff_total > 0 ? "+" : ""}${s.diff_total}</b><span class="s-hint">实盘 - 应盘</span></div>
            </div>
            <div class="toolbar slim">
              <div class="tabs" id="stkFilterTabs">
                <button class="tab${filter === "" ? " is-active" : ""}" data-state="">全部</button>
                <button class="tab${filter === "pending" ? " is-active" : ""}" data-state="pending">未盘</button>
                <button class="tab${filter === "up" ? " is-active" : ""}" data-state="up">盘盈</button>
                <button class="tab${filter === "down" ? " is-active" : ""}" data-state="down">盘亏</button>
                <button class="tab${filter === "match" ? " is-active" : ""}" data-state="match">一致</button>
              </div>
              <div class="spacer"></div>
              <span class="hint">${open ? "录入实盘册数后自动保存" : "盘点单已关闭，明细只读"}</span>
            </div>
            <div class="table-scroll stk-scroll">
              <table class="table" id="stkItems">
                <thead><tr>
                  <th>图书</th><th>条码</th><th class="num">应盘</th><th class="num">实盘</th><th>结果</th>
                </tr></thead>
                <tbody>${(batch.items || [])
                  .map(
                    (it) => `<tr data-item="${it.id}" data-book="${it.book_id}">
                      <td class="wrap title-cell"><strong>${esc(it.book_title)}</strong>
                        <div class="sub">${esc(it.book_author || "—")} · 应盘 ${it.expected_copies} 册</div></td>
                      <td class="sub">${esc(it.book_barcode || "—")}</td>
                      <td class="num">${it.expected_copies}</td>
                      <td class="num">${
                        open
                          ? `<input class="stk-input" type="number" min="0" max="9999" inputmode="numeric"
                                 value="${it.counted_copies === null || it.counted_copies === undefined ? "" : it.counted_copies}"
                                 aria-label="《${esc(it.book_title)}》实盘册数" />`
                          : it.counted_copies === null || it.counted_copies === undefined
                          ? '<span class="sub">未盘</span>'
                          : it.counted_copies
                      }</td>
                      <td>${
                        it.state === "match"
                          ? '<span class="badge success">账实一致</span>'
                          : it.state === "up"
                          ? `<span class="badge warn">盘盈 ${it.diff}</span>`
                          : it.state === "down"
                          ? `<span class="badge danger">盘亏 ${Math.abs(it.diff)}</span>`
                          : '<span class="badge neutral">未盘</span>'
                      }</td>
                    </tr>`
                  )
                  .join("")}</tbody>
              </table>
            </div>
            ${
              open
                ? `<div class="form-note">录入完后点「关闭盘点单」冻结明细，再回到列表点「按差异调账」把实盘数写回库存。逐行失焦即保存。</div>`
                : `<div class="form-note">${batch.status_label}：可「重新打开」继续录入；「按差异调账」会把实盘数写回总册数（不得低于在借册数）。</div>`
            }`,
          confirmText: open ? "关闭盘点单" : "关闭",
          onSubmit: async () => {
            if (open) {
              await api.post(`/api/stocktaking/${batchId}/close`, {});
              toast("盘点单已关闭", "现在可以按差异调账", "ok");
            }
            loadStocktaking().catch(() => {});
          },
          onMount: (form) => {
            form.querySelectorAll("#stkFilterTabs .tab").forEach((tab) =>
              tab.addEventListener("click", () => {
                closeModal();
                openStocktakingDetail(batchId, tab.dataset.state || "");
              })
            );
            form.querySelectorAll("tr[data-item]").forEach((tr) => {
              const input = tr.querySelector(".stk-input");
              if (!input) return;
              let last = input.value;
              input.addEventListener("change", () => {
                if (input.value === last) return;
                const value = input.value.trim();
                api
                  .put(`/api/stocktaking/${batchId}/items/${tr.dataset.item}`, {
                    counted_copies: value === "" ? "" : Number(value),
                  })
                  .then((fresh) => {
                    last = input.value;
                    const item = (fresh.items || []).find((i) => Number(i.id) === Number(tr.dataset.item));
                    if (!item) return;
                    const cell = tr.children[4];
                    cell.innerHTML =
                      item.state === "match"
                        ? '<span class="badge success">账实一致</span>'
                        : item.state === "up"
                        ? `<span class="badge warn">盘盈 ${item.diff}</span>`
                        : item.state === "down"
                        ? `<span class="badge danger">盘亏 ${Math.abs(item.diff)}</span>`
                        : '<span class="badge neutral">未盘</span>';
                  })
                  .catch((err) => {
                    input.value = last;
                    showError(err);
                  });
              });
            });
          },
        });
      })
      .catch(showError);
  }

  function closeStocktaking(batchId) {
    api
      .post(`/api/stocktaking/${batchId}/close`, {})
      .then(() => {
        toast("盘点单已关闭", "可以按差异调账", "ok");
        loadStocktaking().catch(() => {});
      })
      .catch(showError);
  }

  function reopenStocktaking(batchId) {
    api
      .post(`/api/stocktaking/${batchId}/reopen`, {})
      .then(() => {
        toast("已重新打开", "可以继续录入实盘数", "ok");
        loadStocktaking().catch(() => {});
      })
      .catch(showError);
  }

  function applyStocktaking(batchId, code) {
    openModal({
      title: "按差异调账",
      danger: true,
      body: `<div class="confirm-block">
        <p>即将把盘点单 <b>${esc(code)}</b> 的实盘数写回图书总册数。</p>
        <p class="confirm-warn">只处理「已盘且与应盘不符」的图书；实盘数低于在借册数时会整批拒绝，不会产生半截调账。</p>
      </div>`,
      confirmText: "确认调账",
      onSubmit: async () => {
        const result = await api.post(`/api/stocktaking/${batchId}/apply`, { only_diff: true });
        toast("调账完成", result.message || `已调整 ${result.applied} 种`, "ok");
        await loadStocktaking();
        await loadBooks().catch(() => {});
      },
    });
  }

  function deleteStocktaking(batchId, code) {
    openModal({
      title: "删除盘点单",
      danger: true,
      body: `<div class="confirm-block">
        <p>即将删除进行中的盘点单 <b>${esc(code)}</b> 及其全部明细。</p>
        <p class="confirm-warn">已关闭的盘点单不可删除（保留作盘点记录）。</p>
      </div>`,
      confirmText: "确认删除",
      onSubmit: async () => {
        await api.del(`/api/stocktaking/${batchId}`);
        toast("已删除盘点单", code, "ok");
        loadStocktaking().catch(() => {});
      },
    });
  }

  /* ---------------- 采编验收视图 ---------------- */

  function acquisitionBadge(a) {
    if (a.status === "pending") return `<span class="badge info">待验收</span>`;
    if (a.status === "accepted") return `<span class="badge success">已验收</span>`;
    return `<span class="badge neutral">已驳回</span>`;
  }

  async function loadAcquisitions() {
    const s = state.acquisitions;
    await withViewLoading("acquisitions", async () => {
      const payload = await api.get("/api/acquisitions", {
        keyword: s.keyword,
        status: s.status,
        page: s.page,
        page_size: s.pageSize,
      });
      const table = $("#acquisitionTable");
      if (!payload.items.length) {
        const hasFilter = Boolean(s.keyword || (s.status && s.status !== "pending"));
        table.innerHTML = emptyState(8, "没有匹配的验收单", hasFilter ? "清除筛选条件" : "");
        const clear = table.querySelector("[data-clear]");
        if (clear) clear.addEventListener("click", resetAcquisitions);
      } else {
        table.innerHTML = `
          <thead><tr>
            <th>书名 / ISBN</th><th>作者 / 出版社</th><th>分类 / 位置</th>
            <th>册数 / 单价</th><th class="num">金额</th><th>供应商</th><th>状态</th><th>操作</th>
          </tr></thead>
          <tbody>${payload.items
            .map(
              (a) => `<tr>
                <td class="wrap title-cell"><strong>${esc(a.title)}</strong>
                  <div class="sub">${esc(a.isbn || "无 ISBN")}${
                a.existing_book_id ? ` · <span class="tag-inline">库中已有同 ISBN</span>` : ""
              }</div></td>
                <td>${esc(a.author || "—")}<div class="sub">${esc(a.publisher || "—")} ${a.publish_year ? "· " + a.publish_year : ""}</div></td>
                <td>${esc(a.category || "—")}<div class="sub">${esc(a.location || "待分配")}</div></td>
                <td class="num">${a.copies} 册<div class="sub">${money(a.unit_price)} / 册</div></td>
                <td class="num">${money(a.amount)}</td>
                <td class="sub">${esc(a.supplier || "—")}</td>
                <td>${acquisitionBadge(a)}${a.reject_reason ? `<div class="sub">${esc(a.reject_reason)}</div>` : ""}</td>
                <td><div class="actions">
                  ${
                    a.can_accept
                      ? `<button class="btn sm primary" data-accept="${a.id}" data-title="${esc(a.title)}"
                             data-isbn="${esc(a.isbn || "")}" data-existing="${a.existing_book_id || ""}">验收</button>
                         <button class="btn sm danger" data-reject="${a.id}" data-title="${esc(a.title)}">驳回</button>`
                      : `<span class="sub">${esc(a.status_label)}</span>`
                  }
                  <button class="btn sm danger" data-del="${a.id}" data-title="${esc(a.title)}">删除</button>
                </div></td>
              </tr>`
            )
            .join("")}</tbody>`;
      }
      renderPager($("#acquisitionPager"), payload, (patch) => updateFilters(patch));

      table.querySelectorAll("[data-accept]").forEach((btn) =>
        btn.addEventListener("click", () =>
          acceptAcquisition(Number(btn.dataset.accept), {
            title: btn.dataset.title,
            isbn: btn.dataset.isbn,
            existing: btn.dataset.existing,
          })
        )
      );
      table.querySelectorAll("[data-reject]").forEach((btn) =>
        btn.addEventListener("click", () =>
          rejectAcquisition(Number(btn.dataset.reject), btn.dataset.title)
        )
      );
      table.querySelectorAll("[data-del]").forEach((btn) =>
        btn.addEventListener("click", () =>
          deleteAcquisition(Number(btn.dataset.del), btn.dataset.title)
        )
      );
    });
  }

  function resetAcquisitions() {
    state.acquisitions = {
      page: 1,
      pageSize: state.acquisitions.pageSize,
      keyword: "",
      status: "pending",
    };
    syncToolbar("acquisitions");
    syncUrl();
    loadAcquisitions().catch(showError);
  }

  function openAcquisitionForm() {
    api
      .get("/api/stocktaking", { page: 1, page_size: 1 })
      .then((payload) => {
        const locations = payload.locations || [];
        openModal({
          title: "登记到货",
          body: `
            <div class="form-note">登记后先不动作馆藏；验收时才按 ISBN 决定「加册」还是「新建书目」。</div>
            <div class="form-grid">
              <div class="field full"><label>书名<span class="req">*</span></label>
                <input name="title" autocomplete="off" placeholder="必填" /></div>
              <div class="field"><label>ISBN</label>
                <input name="isbn" autocomplete="off" placeholder="选填，验收时据此去重" />
                <span class="tip" id="acqIsbnHint"></span></div>
              <div class="field"><label>到货册数<span class="req">*</span></label>
                <input name="copies" inputmode="numeric" value="1" /></div>
              <div class="field"><label>作者</label><input name="author" autocomplete="off" /></div>
              <div class="field"><label>出版社</label><input name="publisher" autocomplete="off" /></div>
              <div class="field"><label>出版年份</label><input name="publish_year" inputmode="numeric" placeholder="如 2024" /></div>
              <div class="field"><label>分类</label><input name="category" autocomplete="off" placeholder="如 文学 / 计算机" /></div>
              <div class="field"><label>馆藏位置</label>
                <input name="location" autocomplete="off" list="acqLocations" placeholder="如 三楼 A 区" />
                <datalist id="acqLocations">${locations.map((l) => `<option value="${esc(l)}"></option>`).join("")}</datalist>
              </div>
              <div class="field"><label>单价</label><input name="unit_price" inputmode="decimal" placeholder="选填" /></div>
              <div class="field"><label>供应商</label><input name="supplier" autocomplete="off" /></div>
              <div class="field full"><label>备注</label><input name="note" autocomplete="off" placeholder="选填" /></div>
            </div>`,
          confirmText: "登记到货",
          onSubmit: async (form) => {
            const result = await api.post("/api/acquisitions", {
              title: form.title.value,
              isbn: form.isbn.value,
              copies: Number(form.copies.value) || 1,
              author: form.author.value,
              publisher: form.publisher.value,
              publish_year: form.publish_year.value,
              category: form.category.value,
              location: form.location.value,
              unit_price: form.unit_price.value,
              supplier: form.supplier.value,
              note: form.note.value,
            });
            toast("已登记到货", `《${result.title}》${result.copies} 册`, "ok");
            loadAcquisitions().catch(() => {});
          },
        });
      })
      .catch(showError);
  }

  function acceptAcquisition(acqId, info) {
    const existing = Number(info.existing);
    openModal({
      title: "验收入库",
      body: `<div class="form-note">
        《${esc(info.title)}》${info.isbn ? `，ISBN <b>${esc(info.isbn)}</b>` : "，无 ISBN"}
        ${
          existing
            ? `——库中已有同 ISBN 书目，验收后<b>直接加册</b>（不新建重复书目）。`
            : info.isbn
            ? "——库中没有同 ISBN 书目，验收后<b>新建书目</b>并分配馆藏条码。"
            : "——该单没有 ISBN，无法自动建档。请先补填 ISBN（或先在图书管理里新增），再回到本页验收。"
        }
      </div>
      ${
        !existing && info.isbn
          ? `<div class="field full"><label>馆藏条码</label>
               <input name="barcode" autocomplete="off" placeholder="留空则自动分配" />
               <span class="tip">4–32 位字母、数字或连字符；留空由系统按规则生成</span></div>`
          : ""
      }`,
      confirmText: "确认验收",
      onSubmit: async (form) => {
        const barcodeInput = form.querySelector('[name="barcode"]');
        const result = await api.post(`/api/acquisitions/${acqId}/accept`, {
          barcode: barcodeInput ? barcodeInput.value : "",
        });
        const mode = result.mode === "add_copies" ? "已加册" : "已新建书目";
        toast("验收完成", `${mode}：${Number(result.added_copies) || 0} 册`, "ok");
        await loadAcquisitions();
        await loadBooks().catch(() => {});
      },
    });
  }

  function rejectAcquisition(acqId, title) {
    openModal({
      title: "驳回验收单",
      danger: true,
      body: `<div class="confirm-block"><p>即将驳回《${esc(title)}》。</p></div>
        <div class="field full"><label>驳回原因</label>
          <input name="reason" autocomplete="off" placeholder="选填，如「到货破损」「与订单不符」" /></div>`,
      confirmText: "确认驳回",
      onSubmit: async (form) => {
        await api.post(`/api/acquisitions/${acqId}/reject`, { reason: form.reason.value });
        toast("已驳回", title, "ok");
        loadAcquisitions().catch(() => {});
      },
    });
  }

  function deleteAcquisition(acqId, title) {
    openModal({
      title: "删除验收单",
      danger: true,
      body: `<div class="confirm-block"><p>即将删除验收单《${esc(title)}》。</p>
        <p class="confirm-warn">删除后不可恢复；已验收（已入库）的记录不应删除。</p></div>`,
      confirmText: "确认删除",
      onSubmit: async () => {
        await api.del(`/api/acquisitions/${acqId}`);
        toast("已删除验收单", title, "ok");
        loadAcquisitions().catch(() => {});
      },
    });
  }

  function exportAcquisitions() {
    const s = state.acquisitions;
    return withExport($("#acquisitionExport"), async () => {
      const rows = await fetchAllRows("/api/acquisitions", { keyword: s.keyword, status: s.status }, 100);
      if (!rows.length) {
        toast("没有可导出的数据", "当前筛选条件下没有验收单", "warn");
        return;
      }
      downloadCsv(
        `采编验收单-${fileStamp()}.csv`,
        ["登记时间", "书名", "ISBN", "作者", "出版社", "出版年", "分类", "馆藏位置", "册数", "单价", "金额", "供应商", "状态", "经办人", "备注"],
        rows.map((a) => [
          fmtDateTime(a.created_at), a.title, a.isbn, a.author, a.publisher, a.publish_year,
          a.category, a.location, a.copies, Number(a.unit_price).toFixed(2), Number(a.amount).toFixed(2),
          a.supplier, a.status_label, a.operator, a.note,
        ])
      );
      toast("已导出验收单", `共 ${rows.length} 条记录`, "ok");
    });
  }

  /* ---------------- 批量导入视图 ---------------- */

  async function loadImportsView() {
    syncToolbar("imports");
    refreshImportTip();
    const wrap = $("#importResultWrap");
    if (wrap) wrap.hidden = true;
  }

  function refreshImportTip() {
    const kind = state.imports.kind;
    const tip = $("#importTip");
    const hint = $("#importHint");
    const isBooks = kind === "books";
    const columns = isBooks
      ? "书名（必填）、ISBN、作者、出版社、分类、出版年、馆藏位置、总册数、馆藏条码、图书简介"
      : "姓名（必填）、读者证号（必填）、读者类型、手机号、邮箱、可借上限、状态";
    if (hint) hint.textContent = isBooks ? "图书批量建档" : "读者批量建档（初始密码为证号后 6 位）";
    if (state.imports.xlsx) {
      tipNode(
        tip,
        `已载入 Excel「${state.imports.fileName}」，点「预览校验」查看逐行结果（Excel 会自动识别第一张工作表）。`,
        "ok"
      );
      return;
    }
    tipNode(
      tip,
      `当前为「${isBooks ? "图书" : "读者"}」导入。表头列：${columns}。支持 .csv 与 .xlsx（Excel 请直接上传文件，不要复制粘贴成图片）；中英文表头都能识别；先「预览校验」看逐行结果，确认无误再「开始导入」。`,
      ""
    );
  }

  function downloadImportTemplate() {
    const kind = state.imports.kind;
    const btn = $("#importTemplate");
    btn.disabled = true;
    fetch(`/api/import/${kind}/template`, { credentials: "same-origin" })
      .then((r) => {
        if (!r.ok) throw new ApiError("模板下载失败", r.status, "http_error");
        return r.blob();
      })
      .then((blob) => {
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = `${kind === "books" ? "图书" : "读者"}导入模板.csv`;
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
        toast("模板已下载", "用 Excel / WPS 打开即可填写", "ok");
      })
      .catch(showError)
      .finally(() => {
        btn.disabled = false;
      });
  }

  function renderImportResult(result) {
    const wrap = $("#importResultWrap");
    const table = $("#importResultTable");
    const rows = result.rows || [];
    wrap.hidden = false;
    const failed = rows.filter((r) => r.status === "failed");
    table.innerHTML = `
      <thead><tr><th>行号</th><th>对象</th><th>结果</th><th>说明</th></tr></thead>
      <tbody>${rows
        .map(
          (r) => `<tr class="${r.status === "failed" ? "row-bad" : ""}">
            <td class="num">${r.line}</td>
            <td class="wrap">${esc(r.label || "—")}</td>
            <td>${
              r.status === "failed"
                ? '<span class="badge danger">失败</span>'
                : '<span class="badge success">通过</span>'
            }</td>
            <td class="wrap">${esc(r.message || "")}</td>
          </tr>`
        )
        .join("")}</tbody>`;
    tipNode(
      $("#importTip"),
      `${result.message}${failed.length ? "。下方标红的行为失败原因，修正后可重试。" : ""}`,
      result.failed ? "warn" : "ok"
    );
  }

  async function runImport(dryRun) {
    const kind = state.imports.kind;
    const text = $("#importPaste").value || "";
    const usingExcel = Boolean(state.imports.xlsx);
    if (!usingExcel && !text.trim()) {
      tipNode($("#importTip"), "请先粘贴表格内容，或选择 .csv / .xlsx 文件", "warn");
      return;
    }
    const btn = dryRun ? $("#importPreview") : $("#importRun");
    const original = btn.textContent;
    btn.disabled = true;
    btn.textContent = dryRun ? "校验中…" : "导入中…";
    try {
      const payload = usingExcel ? { xlsx: state.imports.xlsx } : { csv: text };
      payload.dry_run = Boolean(dryRun);
      const result = await api.post(`/api/import/${kind}`, payload);
      renderImportResult(result);
      if (!dryRun && result.created > 0) {
        toast("导入完成", `新增 ${result.created} 条，失败 ${result.failed} 条`, "ok");
        if (kind === "books") loadBooks().catch(() => {});
        else loadMembers().catch(() => {});
        loadCategories().catch(() => {});
      } else if (dryRun) {
        toast("校验完成", `${result.total - result.failed}/${result.total} 行通过`, result.failed ? "warn" : "ok");
      }
    } catch (err) {
      showError(err);
      tipNode($("#importTip"), err.message || "导入失败", "warn");
    } finally {
      btn.disabled = false;
      btn.textContent = original;
    }
  }

  /** 选择文件：.xlsx 读成 base64 交后端解，其余按文本读（CSV / 制表符文本）。 */
  function pickImportFile(file) {
    const name = String(file.name || "");
    const isExcel = /\.xlsx$/i.test(name);
    if (isExcel) {
      const reader = new FileReader();
      reader.onload = () => {
        const dataUrl = String(reader.result || "");
        const base64 = dataUrl.slice(dataUrl.indexOf(",") + 1);
        state.imports.xlsx = base64;
        state.imports.fileName = name;
        const paste = $("#importPaste");
        if (paste) paste.value = "";
        const wrap = $("#importResultWrap");
        if (wrap) wrap.hidden = true;
        refreshImportTip();
      };
      reader.onerror = () => tipNode($("#importTip"), "Excel 读取失败，请重试或先另存为 CSV", "warn");
      reader.readAsDataURL(file);
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      // utf-8-sig 兼容 Excel 导出的 BOM；后续解析由服务端统一处理
      state.imports.xlsx = "";
      state.imports.fileName = "";
      $("#importPaste").value = String(reader.result || "");
      tipNode($("#importTip"), `已载入文件「${name}」，点「预览校验」查看逐行结果`, "ok");
    };
    reader.onerror = () => tipNode($("#importTip"), "文件读取失败，请重试或改用粘贴", "warn");
    reader.readAsText(file, "utf-8");
  }

  /* ---------------- 批量催还 ---------------- */

  function notifyOverdue() {
    const s = state.loans;
    const inOverdueTab = s.status === "overdue";
    openModal({
      title: "批量催还",
      body: `<div class="form-note">
        ${
          inOverdueTab
            ? "将对当前筛选条件下的**全部逾期**读者各发一条站内通知（阅读来源：消息中心）。"
            : "当前不在「逾期」页签。将按下方关键词（留空 = 全部逾期）匹配所有逾期记录并发通知。"
        }
        同一笔借阅的同一次逾期只提醒一次，反复点击不会给读者刷屏。
      </div>
      <div class="field full"><label>关键词（可选）</label>
        <input name="keyword" autocomplete="off" value="${esc(inOverdueTab ? s.keyword : "")}"
               placeholder="书名 / 条码 / 读者 / 证号，留空 = 全部逾期" />
      </div>`,
      confirmText: "发送催还通知",
      onSubmit: async (form) => {
        const result = await api.post("/api/loans/notify-overdue", {
          keyword: form.keyword.value,
        });
        if (!result.requested) {
          toast("没有需要催还的记录", "当前没有匹配的逾期借阅", "warn");
        } else {
          toast(
            "催还已处理",
            `匹配 ${result.requested} 条，新发送 ${result.sent} 条${result.skipped ? `，已提醒过 ${result.skipped} 条` : ""}`,
            "ok"
          );
        }
      },
    });
  }

  /* ---------------- 打印：借书凭证 / 催还单 ---------------- */

  function printLoanReceipt(info) {
    $("#printLayer").innerHTML = `
      <div class="print-sheet">
        <div class="ps-head">
          <div class="ps-lib">${esc(LIBRARY_NAME)}</div>
          <div class="ps-kind">借书凭证</div>
        </div>
        <table class="ps-table">
          <tr><th>读者姓名</th><td>${esc(info.member_name || "—")}</td>
              <th>借书证号</th><td>${esc(info.member_card || "—")}</td></tr>
          <tr><th>书名</th><td colspan="3">${esc(info.book_title || "—")}</td></tr>
          <tr><th>馆藏条码</th><td>${esc(info.book_barcode || "—")}</td>
              <th>ISBN</th><td>${esc(info.book_isbn || "—")}</td></tr>
          <tr><th>借出日期</th><td>${esc(fmtDate(info.borrowed_at))}</td>
              <th>应还日期</th><td class="ps-due">${esc(fmtDate(info.due_at))}</td></tr>
        </table>
        <div class="ps-note">请按期归还，逾期将按日计收滞纳金。凭本凭证与借书证办理还书。</div>
        <div class="ps-sign">
          <span>经办人：__________</span><span>读者签字：__________</span><span>日期：${esc(fmtDate(info.borrowed_at))}</span>
        </div>
      </div>`;
    window.print();
  }

  function printOverdueSlips() {
    const s = state.loans;
    withExport($("#loanPrintOverdue"), async () => {
      const rows = await fetchAllRows(
        "/api/loans",
        { keyword: s.keyword, status: "overdue", sort: "due_at", order: "asc" },
        100
      );
      if (!rows.length) {
        toast("没有可打印的催还单", "当前筛选条件下没有逾期记录", "warn");
        return;
      }
      $("#printLayer").innerHTML = rows
        .map(
          (l) => `<div class="print-sheet">
            <div class="ps-head">
              <div class="ps-lib">${esc(LIBRARY_NAME)}</div>
              <div class="ps-kind">图书催还单</div>
            </div>
            <table class="ps-table">
              <tr><th>读者姓名</th><td>${esc(l.member_name)}</td>
                  <th>借书证号</th><td>${esc(l.member_card)}</td></tr>
              <tr><th>书名</th><td colspan="3">${esc(l.book_title)}</td></tr>
              <tr><th>馆藏条码</th><td>${esc(l.book_barcode || "—")}</td>
                  <th>应还日期</th><td class="ps-due">${esc(fmtDate(l.due_at))}</td></tr>
              <tr><th>已逾期</th><td>${Number(l.overdue_days) || 0} 天</td>
                  <th>应缴滞纳金</th><td>${money(l.fine)}</td></tr>
            </table>
            <div class="ps-note">您所借图书已逾期，请尽快到馆归还并结清滞纳金。逾期期间无法续借。</div>
            <div class="ps-sign">
              <span>经办人：__________</span><span>送出日期：${esc(fmtDate(new Date().toISOString()))}</span>
            </div>
          </div>`
        )
        .join("");
      toast("已生成催还单", `共 ${rows.length} 张，请在打印预览中确认`, "ok");
      window.print();
    });
  }

  /* ---------------- 六个视图补齐 CSV 导出 ---------------- */

  function exportReservations() {
    const s = state.reservations;
    return withExport($("#reservationExport"), async () => {
      const rows = await fetchAllRows("/api/reservations", { keyword: s.keyword, status: s.status }, 100);
      if (!rows.length) {
        toast("没有可导出的数据", "当前筛选条件下没有预约记录", "warn");
        return;
      }
      downloadCsv(
        `预约队列-${fileStamp()}.csv`,
        ["图书", "馆藏条码", "读者", "借书证号", "预约时间", "状态", "排队位次", "到书时间", "保留到期"],
        rows.map((r) => [
          r.book_title, r.book_barcode, r.member_name, r.member_card,
          fmtDateTime(r.created_at), r.status_label || r.status,
          Number(r.queue_position) || "", fmtDateTime(r.ready_at), r.expires_at ? fmtDate(r.expires_at) : "",
        ])
      );
      toast("已导出预约队列", `共 ${rows.length} 条记录`, "ok");
    });
  }

  function exportLogs() {
    const s = state.logs;
    return withExport($("#logExport"), async () => {
      const rows = await fetchAllRows(
        "/api/logs",
        { keyword: s.keyword, actor_type: s.actorType, action: s.action, status: s.status },
        100
      );
      if (!rows.length) {
        toast("没有可导出的数据", "当前筛选条件下没有日志", "warn");
        return;
      }
      downloadCsv(
        `操作日志-${fileStamp()}.csv`,
        ["时间", "操作者", "类型", "动作", "对象", "结果", "摘要"],
        rows.map((l) => [
          fmtDateTime(l.created_at), l.actor_name || "", l.actor_type_label || l.actor_type,
          l.action_label || l.action, l.entity_label || l.entity || "",
          l.status === "denied" ? "已拦截" : "已执行", l.summary,
        ])
      );
      toast("已导出操作日志", `共 ${rows.length} 条记录`, "ok");
    });
  }

  function exportAnnouncements() {
    const s = state.announcements;
    return withExport($("#announcementExport"), async () => {
      const rows = await fetchAllRows(
        "/api/announcements",
        { keyword: s.keyword, status: s.status, kind: s.kind },
        100
      );
      if (!rows.length) {
        toast("没有可导出的数据", "当前筛选条件下没有公告", "warn");
        return;
      }
      downloadCsv(
        `公告清单-${fileStamp()}.csv`,
        ["标题", "类型", "重要程度", "生效窗口", "状态", "置顶", "正文", "更新时间"],
        rows.map((a) => [
          a.title, a.kind_label, a.level_label || a.level, a.window_label,
          a.status_label || a.status, a.pinned ? "是" : "否", a.body, fmtDateTime(a.updated_at),
        ])
      );
      toast("已导出公告清单", `共 ${rows.length} 条记录`, "ok");
    });
  }

  function exportApplications() {
    const s = state.applications;
    return withExport($("#applicationExport"), async () => {
      const rows = await fetchAllRows(
        "/api/applications",
        { keyword: s.keyword, status: s.status },
        100
      );
      if (!rows.length) {
        toast("没有可导出的数据", "当前筛选条件下没有办证申请", "warn");
        return;
      }
      downloadCsv(
        `办证申请-${fileStamp()}.csv`,
        ["申请编号", "姓名", "手机号", "证件类型", "证件后四位", "读者类型", "提交时间", "状态", "审核人", "驳回原因"],
        rows.map((a) => [
          a.code, a.name, a.phone_masked, a.id_type, a.id_tail_display, a.member_type,
          fmtDateTime(a.created_at), a.status_label || a.status, a.reviewed_by, a.reject_reason,
        ])
      );
      toast("已导出办证申请", `共 ${rows.length} 条记录`, "ok");
    });
  }

  function exportEngagementRows() {
    const s = state.engagement;
    return withExport($("#engagementExport"), async () => {
      const kind = s.kind;
      const endpoint =
        kind === "book-requests"
          ? "/api/engagement/book-requests"
          : kind === "profile-requests"
          ? "/api/engagement/profile-requests"
          : "/api/engagement/reviews";
      const rows = await fetchAllRows(endpoint, { keyword: s.keyword, status: s.status }, 100);
      if (!rows.length) {
        toast("没有可导出的数据", "当前筛选条件下没有记录", "warn");
        return;
      }
      if (kind === "book-requests") {
        downloadCsv(
          `荐购登记-${fileStamp()}.csv`,
          ["书名", "作者", "ISBN", "出版社", "读者", "借书证号", "荐购理由", "答复", "状态", "提交时间"],
          rows.map((r) => [
            r.title, r.author, r.isbn, r.publisher, r.member_name, r.member_card,
            r.reason, r.reply, r.status_label || r.status, fmtDateTime(r.created_at),
          ])
        );
      } else if (kind === "profile-requests") {
        downloadCsv(
          `资料变更申请-${fileStamp()}.csv`,
          ["读者", "借书证号", "变更字段", "原值", "新值", "理由", "答复", "状态", "提交时间"],
          rows.map((r) => [
            r.member_name, r.member_card, r.field_label, r.old_value, r.new_value,
            r.reason, r.reply, r.status_label || r.status, fmtDateTime(r.created_at),
          ])
        );
      } else {
        downloadCsv(
          `图书评价-${fileStamp()}.csv`,
          ["图书", "作者", "读者", "评分", "短评", "状态", "提交时间"],
          rows.map((r) => [
            r.book_title, r.book_author, r.member_display || r.member_name,
            r.rating, r.content, r.status_label || r.status, fmtDateTime(r.created_at),
          ])
        );
      }
      toast("已导出", `共 ${rows.length} 条记录`, "ok");
    });
  }

  /* ---------------- 读者附件（照片 / 证件） ---------------- */

  const FILE_KINDS = [
    { key: "photo", label: "读者照片" },
    { key: "id_card", label: "证件影像" },
  ];

  function openMemberFilesDialog(memberId, memberName) {
    api
      .get("/api/member-files", { member_id: memberId })
      .then((payload) => {
        const files = payload.items || [];
        openModal({
          title: `附件 · ${memberName}`,
          body: `
            <div class="form-note">支持 JPG / PNG / WebP / PDF，单个不超过 ${payload.max_size_mb || 4}MB，每类最多 ${payload.max_files_per_kind || 10} 个。附件是个人信息，只有后台登录态可下载。</div>
            <div class="form-grid">
              <div class="field"><label>附件类型</label>
                <select name="kind">
                  ${FILE_KINDS.map((k) => `<option value="${k.key}">${k.label}</option>`).join("")}
                </select>
              </div>
              <div class="field"><label>选择文件</label>
                <input type="file" name="file" accept="image/jpeg,image/png,image/webp,application/pdf" />
              </div>
            </div>
            <div class="file-list" id="fileList">
              ${
                files.length
                  ? files
                      .map(
                        (f) => `<div class="file-item" data-id="${f.id}">
                          ${
                            f.is_image
                              ? `<img class="fi-thumb" src="${esc(f.url)}" alt="${esc(f.kind_label)}" loading="lazy" />`
                              : `<span class="fi-thumb is-doc">PDF</span>`
                          }
                          <div class="fi-main">
                            <span class="fi-kind">${esc(f.kind_label)}</span>
                            <b>${esc(f.filename)}</b>
                            <span class="sub">${esc(f.size_label || "")} · ${esc(fmtDateTime(f.created_at))}</span>
                          </div>
                          <div class="fi-actions">
                            <a class="btn sm ghost" href="${esc(f.url)}" target="_blank" rel="noopener">查看</a>
                            <button class="btn sm danger" type="button" data-del="${f.id}">删除</button>
                          </div>
                        </div>`
                      )
                      .join("")
                  : `<div class="pick-empty">还没有上传任何附件</div>`
              }
            </div>`,
          confirmText: "上传所选文件",
          onSubmit: async (form) => {
            const fileInput = form.querySelector('[name="file"]');
            const file = fileInput && fileInput.files && fileInput.files[0];
            if (!file) throw new ApiError("请先选择要上传的文件", 400, "validation_error", "file");
            const fd = new FormData();
            fd.append("member_id", String(memberId));
            fd.append("kind", form.kind.value);
            fd.append("file", file);
            const res = await fetch("/api/member-files", {
              method: "POST",
              credentials: "same-origin",
              headers: auth.csrf ? { "X-CSRF-Token": auth.csrf } : {},
              body: fd,
            });
            const payload = await res.json().catch(() => null);
            if (!res.ok) {
              const err = (payload && payload.error) || {};
              throw new ApiError(err.message || "上传失败", res.status, err.code || "http_error");
            }
            toast("上传成功", file.name, "ok");
            loadMembers().catch(() => {});
          },
          onMount: (form) => {
            form.querySelectorAll("[data-del]").forEach((btn) =>
              btn.addEventListener("click", () => {
                api
                  .del(`/api/member-files/${btn.dataset.del}`)
                  .then(() => {
                    toast("已删除附件", "", "ok");
                    closeModal();
                    openMemberFilesDialog(memberId, memberName);
                  })
                  .catch(showError);
              })
            );
          },
        });
      })
      .catch(showError);
  }

  /* ---------------- 审批中心 ---------------- */

  const APPROVAL_BADGES = {
    pending: ["warn", "待审批"],
    approved: ["ok", "已通过"],
    rejected: ["neutral", "已驳回"],
  };

  function approvalStatusBadge(item) {
    const [cls, label] = APPROVAL_BADGES[item.status] || ["neutral", item.status_label || item.status];
    return `<span class="badge ${cls}">${esc(label)}</span>`;
  }

  /** 导航角标：待审批数量（失败静默，角标只是锦上添花）。 */
  async function refreshApprovalBadge() {
    const badge = $("#approvalBadge");
    if (!badge || !can("approval:approve")) return;
    try {
      const data = await api.get("/api/approvals/pending-count");
      const total = Number(data.pending) || 0;
      badge.hidden = total <= 0;
      badge.textContent = total > 99 ? "99+" : String(total);
    } catch (err) {
      badge.hidden = true;
    }
  }

  async function loadApprovals() {
    syncToolbar("approvals");
    const s = state.approvals;
    await withViewLoading("approvals", async () => {
      const data = await api.get("/api/approvals", {
        status: s.status,
        keyword: s.keyword,
        page: s.page,
        page_size: s.pageSize,
      });
      renderApprovals(data);
      refreshApprovalBadge();
    });
  }

  function renderApprovals(data) {
    const table = $("#approvalTable");
    const items = data.items || [];
    const hint = $("#approvalHint");
    if (hint) hint.textContent = `待审批 ${data.pending} 条`;
    if (!items.length) {
      // 文案要看**当前的筛选条件**（state），不是响应体——接口返回里没有 status 字段，
      // 早先写成 data.status 会让「没有待审批的工单」这半句永远不生效。
      table.innerHTML = emptyState(
        7,
        state.approvals.status === "pending" ? "没有待审批的工单" : "没有符合条件的审批记录",
        state.approvals.keyword ? "清除筛选" : ""
      );
      const clear = table.querySelector("[data-clear]");
      if (clear) clear.addEventListener("click", resetApprovals);
    } else {
      const rows = items
        .map((item) => {
          const reviewed = item.reviewed_at
            ? `<div class="sub">${esc(fmtDateTime(item.reviewed_at))}</div>`
            : "";
          const actions =
            item.status === "pending"
              ? `<button class="btn sm primary" data-approve="${item.id}">通过</button>
                 <button class="btn sm ghost" data-reject="${item.id}">驳回</button>`
              : `<span class="sub">已处理</span>`;
          return `<tr>
            <td><b>${esc(item.kind_label)}</b><div class="sub">${esc(item.entity_label || "")}</div></td>
            <td>${esc(item.member_name || "—")}<div class="sub">${esc(item.card_no || "")}</div></td>
            <td><b>${money(item.amount)}</b></td>
            <td>${esc(item.requested_by || "—")}<div class="sub">${esc(fmtDateTime(item.requested_at))}</div></td>
            <td>${approvalStatusBadge(item)}<div class="sub">${esc(item.note || "")}</div></td>
            <td>${esc(item.reviewed_by || "—")}${reviewed}<div class="sub">${esc(item.review_note || "")}</div></td>
            <td class="actions">${actions}</td>
          </tr>`;
        })
        .join("");
      table.innerHTML = `<thead><tr>${LOADING.approvals.headers
        .map((h) => `<th>${esc(h)}</th>`)
        .join("")}</tr></thead><tbody>${rows}</tbody>`;
      table.querySelectorAll("[data-approve]").forEach((btn) =>
        btn.addEventListener("click", () => {
          const item = items.find((i) => String(i.id) === btn.dataset.approve);
          if (item) openApprovalReview(item, true);
        })
      );
      table.querySelectorAll("[data-reject]").forEach((btn) =>
        btn.addEventListener("click", () => {
          const item = items.find((i) => String(i.id) === btn.dataset.reject);
          if (item) openApprovalReview(item, false);
        })
      );
    }
    renderPager($("#approvalPager"), data, (patch) => updateFilters(patch));
  }

  /** 审批确认：通过会**立即执行**减免，所以把金额与理由再摆一次给审批人看。 */
  function openApprovalReview(item, approve) {
    openModal({
      title: approve ? "通过审批 · 罚金减免" : "驳回审批 · 罚金减免",
      danger: !approve,
      confirmText: approve ? "确认通过并执行" : "确认驳回",
      body: `
        <div class="confirm-block">
          <p>${esc(item.entity_label || "")}</p>
          <p class="confirm-amount">${money(item.amount)}</p>
          <p class="confirm-reason">申请理由：${esc(item.note || "—")}</p>
          <p class="confirm-warn">${
            approve
              ? "通过后立即执行减免（与审批状态同一事务），并记入操作日志。"
              : "驳回不会改动任何业务数据，意见会留档在工单上。"
          }</p>
        </div>
        <div class="field">
          <label for="approvalNote">审批意见${approve ? "（选填）" : "（必填）"}</label>
          <textarea id="approvalNote" rows="3" placeholder="${approve ? "可补充说明" : "请说明驳回原因"}"></textarea>
        </div>`,
      onSubmit: async () => {
        const note = ($("#approvalNote").value || "").trim();
        if (!approve && !note) {
          throw new ApiError("驳回必须填写审批意见", 400, "validation_error", "review_note");
        }
        const result = await api.post(`/api/approvals/${item.id}/${approve ? "approve" : "reject"}`, {
          review_note: note,
        });
        toast(
          approve ? "已通过" : "已驳回",
          approve ? result.message || "减免已执行" : "工单已驳回，业务数据未改动",
          approve ? "ok" : "warn"
        );
        await loadApprovals();
      },
    });
  }

  function resetApprovals() {
    state.approvals.keyword = "";
    state.approvals.status = "pending";
    state.approvals.page = 1;
    syncUrl();
    loadApprovals().catch(showError);
  }

  /* ---------------- 系统维护 ---------------- */

  async function loadSystem() {
    syncToolbar("system");
    const pane = state.system.pane || "jobs";
    if (pane === "mail") return loadSystemMail();
    if (pane === "backups") return loadSystemBackups();
    return loadSystemJobs();
  }

  async function loadSystemJobs() {
    const data = await api.get("/api/system/jobs");
    state.system.jobs = data;
    renderSumGrid($("#jobSummary"), [
      {
        label: "后台线程",
        value: data.enabled ? (data.thread_running ? "运行中" : "已停止") : "未启用",
        hint: data.enabled ? `每 ${data.interval_seconds} 秒一轮` : "惰性模式（打开页面才补算）",
        tone: data.enabled ? (data.thread_running ? "ok" : "danger") : "warn",
      },
      { label: "累计执行", value: `${data.total_runs} 轮`, hint: "含定时与手动触发" },
      {
        label: "最近一轮",
        value: data.last_run ? `${data.last_run.duration_ms || 0} ms` : "—",
        hint: data.last_run ? fmtDateTime(data.last_run.started_at) : "还没有执行记录",
        tone: data.last_run ? (data.last_run.ok ? "ok" : "danger") : "",
      },
    ]);

    const table = $("#jobTable");
    const rows = (data.recent || [])
      .map((run) => {
        const result = run.result_data || {};
        const detail = Object.entries(result)
          .map(([key, value]) => `${JOB_FIELD_LABELS[key] || key} ${value}`)
          .join(" · ");
        return `<tr>
          <td>${esc(fmtDateTime(run.started_at))}</td>
          <td>${esc(run.kind_label)}</td>
          <td>${run.duration_ms === null || run.duration_ms === undefined ? "—" : `${run.duration_ms} ms`}</td>
          <td>${run.ok ? `<span class="badge ok">成功</span>` : `<span class="badge danger">有异常</span>`}</td>
          <td>${esc(detail || "—")}</td>
          <td><div class="sub">${esc(run.error || "")}</div></td>
        </tr>`;
      })
      .join("");
    table.innerHTML = `<thead><tr>${["开始时间", "触发方式", "耗时", "结果", "本轮计数", "异常"]
      .map((h) => `<th>${h}</th>`)
      .join("")}</tr></thead>
      <tbody>${
        rows ||
        `<tr><td class="empty" colspan="6">还没有执行记录。后台线程启用后每轮都会留痕，也可以点「立即执行一轮」。</td></tr>`
      }</tbody>`;
    const hint = $("#systemHint");
    if (hint) hint.textContent = data.enabled ? "" : "当前为惰性模式（LIBRARY_SWEEP_INTERVAL_SECONDS=0）";
  }

  const JOB_FIELD_LABELS = {
    expired_reservations: "释放预约",
    members: "扫描读者",
    due_soon: "到期提醒",
    overdue: "逾期提醒",
    mail: "邮件投递",
  };

  async function runJobNow() {
    const btn = $("#jobRunNow");
    btn.disabled = true;
    try {
      const data = await api.post("/api/system/jobs/sweep/run", {});
      const result = data.result || {};
      const detail = Object.entries(result)
        .map(([key, value]) => `${JOB_FIELD_LABELS[key] || key} ${value}`)
        .join(" · ");
      toast("已执行一轮", detail || "本轮没有需要处理的数据", "ok");
      await loadSystemJobs();
    } catch (err) {
      showError(err);
    } finally {
      btn.disabled = false;
    }
  }

  async function loadSystemMail() {
    const data = await api.get("/api/system/mail");
    state.system.mail = data;
    renderSumGrid($("#mailSummary"), [
      {
        label: "渠道状态",
        value: data.configured ? "已配置" : "未配置",
        hint: data.configured ? `${data.host}:${data.port}（${data.secure.toUpperCase()}）` : "设置 LIBRARY_SMTP_HOST 后启用",
        tone: data.configured ? "ok" : "warn",
      },
      { label: "发件地址", value: data.from_address || "—", hint: data.username ? `登录账号 ${data.username}` : "" },
      { label: "待发送", value: `${data.counts.pending} 封`, hint: "后台扫描时自动投递", tone: data.counts.pending ? "info" : "" },
      { label: "已发送", value: `${data.counts.sent} 封`, hint: "累计成功投递", tone: "ok" },
      {
        label: "发送失败",
        value: `${data.counts.failed} 封`,
        hint: data.counts.failed ? "可在下方一键重试" : "",
        tone: data.counts.failed ? "danger" : "",
      },
    ]);

    const table = $("#mailTable");
    const rows = (data.recent || [])
      .map(
        (m) => `<tr>
          <td>${esc(fmtDateTime(m.created_at))}</td>
          <td>${esc(m.member_name || "—")}<div class="sub">${esc(m.to_address || "")}</div></td>
          <td>${esc(m.subject || "")}</td>
          <td>${mailStatusBadge(m, data.status_labels)}</td>
          <td>${m.attempts || 0}</td>
          <td><div class="sub">${esc(m.last_error || "")}</div></td>
        </tr>`
      )
      .join("");
    table.innerHTML = `<thead><tr>${["时间", "收件人", "主题", "状态", "尝试", "错误"]
      .map((h) => `<th>${h}</th>`)
      .join("")}</tr></thead>
      <tbody>${
        rows || `<tr><td class="empty" colspan="6">还没有邮件记录。读者触发催还 / 到期 / 到书通知且留有邮箱时，这里会出现待发送邮件。</td></tr>`
      }</tbody>`;
    if (!data.configured) {
      tipNode($("#mailHint"), "未配置 SMTP：邮件会排队等待，配置后自动补发", "warn");
    } else {
      tipNode($("#mailHint"), "", "");
    }
  }

  function mailStatusBadge(item, labels) {
    const map = { sent: "ok", pending: "warn", failed: "danger" };
    const label = (labels && labels[item.status]) || item.status;
    return `<span class="badge ${map[item.status] || "neutral"}">${esc(label)}</span>`;
  }

  async function sendTestMail() {
    const to = ($("#mailTestTo").value || "").trim();
    if (!to) {
      tipNode($("#mailHint"), "请先填写收件邮箱", "warn");
      return;
    }
    const btn = $("#mailTest");
    btn.disabled = true;
    try {
      await api.post("/api/system/mail/test", { to });
      toast("测试邮件已发送", `请查收 ${to}`, "ok");
      tipNode($("#mailHint"), `测试邮件已发往 ${to}`, "ok");
      await loadSystemMail();
    } catch (err) {
      showError(err);
      tipNode($("#mailHint"), err.message || "发送失败", "warn");
    } finally {
      btn.disabled = false;
    }
  }

  async function retryFailedMail() {
    const btn = $("#mailRetry");
    btn.disabled = true;
    try {
      const data = await api.post("/api/system/mail/retry", {});
      toast(data.reset ? "已重新入队" : "没有失败的邮件", data.reset ? `${data.reset} 封邮件将在下一轮投递` : "队列里没有发送失败的邮件", data.reset ? "ok" : "warn");
      await loadSystemMail();
    } catch (err) {
      showError(err);
    } finally {
      btn.disabled = false;
    }
  }

  async function loadSystemBackups() {
    const data = await api.get("/api/system/backups");
    state.system.backups = data.items || [];
    const table = $("#backupTable");
    const rows = (data.items || [])
      .map(
        (b) => `<tr>
          <td><b>${esc(b.name)}</b></td>
          <td>${esc(b.size_label)}</td>
          <td>${b.kind === "zip" ? '<span class="badge info">含附件</span>' : '<span class="badge neutral">仅数据库</span>'}</td>
          <td>${esc(b.created_at || "—")}</td>
          <td>${
            b.counts && b.counts.books !== undefined
              ? `书目 ${b.counts.books} · 读者 ${b.counts.members} · 借阅 ${b.counts.loans}`
              : "—"
          }</td>
          <td class="actions"><button class="btn sm ghost" data-download="${esc(b.name)}">下载</button></td>
        </tr>`
      )
      .join("");
    table.innerHTML = `<thead><tr>${["备份文件", "大小", "类型", "创建时间", "数据量", "操作"]
      .map((h) => `<th>${h}</th>`)
      .join("")}</tr></thead>
      <tbody>${
        rows || `<tr><td class="empty" colspan="6">还没有备份。建议每天营业前点一次「立即备份」，重要操作前也备一次。</td></tr>`
      }</tbody>`;
    table.querySelectorAll("[data-download]").forEach((btn) =>
      btn.addEventListener("click", () => downloadBackup(btn.dataset.download))
    );
    const hint = $("#backupHint");
    if (hint) hint.textContent = `备份目录：${data.directory}`;
  }

  function downloadBackup(name) {
    const link = document.createElement("a");
    link.href = `/api/system/backups/${encodeURIComponent(name)}`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    toast("开始下载", name, "ok");
  }

  async function createBackup() {
    const btn = $("#backupCreate");
    const withUploads = $("#backupWithUploads") && $("#backupWithUploads").checked;
    btn.disabled = true;
    btn.textContent = "备份中…";
    try {
      const result = await api.post("/api/system/backups", { with_uploads: withUploads });
      toast("备份完成", `${result.name}（${result.size_label}）`, "ok");
      await loadSystemBackups();
    } catch (err) {
      showError(err);
    } finally {
      btn.disabled = false;
      btn.textContent = "立即备份";
    }
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

    // 预约管理（仅具备 loan:write 的账号可见，节点由 applyPermissions 控制显隐）
    const reservationTabs = $("#reservationTabs");
    if (reservationTabs) {
      reservationTabs.addEventListener("click", (e) => {
        const tab = e.target.closest(".tab");
        if (!tab) return;
        updateFilters({ status: tab.dataset.status, page: 1 });
      });
    }
    const reservationKw = $("#reservationKeyword");
    if (reservationKw) {
      reservationKw.addEventListener(
        "input",
        debounce((e) => updateFilters({ keyword: e.target.value.trim(), page: 1 }), 300)
      );
    }
    const reservationResetBtn = $("#reservationReset");
    if (reservationResetBtn) reservationResetBtn.addEventListener("click", resetReservations);

    // 办证审核（仅具备 member:write 的账号可见）
    const applicationTabs = $("#applicationTabs");
    if (applicationTabs) {
      applicationTabs.addEventListener("click", (e) => {
        const tab = e.target.closest(".tab");
        if (!tab) return;
        updateFilters({ status: tab.dataset.status, page: 1 });
      });
    }
    const applicationKw = $("#applicationKeyword");
    if (applicationKw) {
      applicationKw.addEventListener(
        "input",
        debounce((e) => updateFilters({ keyword: e.target.value.trim(), page: 1 }), 300)
      );
    }
    const applicationResetBtn = $("#applicationReset");
    if (applicationResetBtn) applicationResetBtn.addEventListener("click", resetApplications);

    // 公告管理（仅具备 announcement:write 的账号可见）
    const announcementTabs = $("#announcementTabs");
    if (announcementTabs) {
      announcementTabs.addEventListener("click", (e) => {
        const tab = e.target.closest(".tab");
        if (!tab) return;
        updateFilters({ status: tab.dataset.status, page: 1 });
      });
    }
    const announcementKind = $("#announcementKind");
    if (announcementKind) {
      announcementKind.addEventListener("change", (e) => updateFilters({ kind: e.target.value, page: 1 }));
    }
    const announcementKw = $("#announcementKeyword");
    if (announcementKw) {
      announcementKw.addEventListener(
        "input",
        debounce((e) => updateFilters({ keyword: e.target.value.trim(), page: 1 }), 300)
      );
    }
    const announcementResetBtn = $("#announcementReset");
    if (announcementResetBtn) announcementResetBtn.addEventListener("click", resetAnnouncements);
    const announcementCreateBtn = $("#announcementCreate");
    if (announcementCreateBtn) announcementCreateBtn.addEventListener("click", () => openAnnouncementForm(null));

    // 读者互动（评价 / 荐购 / 资料变更三类共用一个视图）
    const engagementKindTabs = $("#engagementKindTabs");
    if (engagementKindTabs) {
      engagementKindTabs.addEventListener("click", (e) => {
        const tab = e.target.closest(".tab");
        if (!tab || tab.dataset.kind === state.engagement.kind) return;
        const kind = ENGAGEMENT_KINDS[tab.dataset.kind] || ENGAGEMENT_KINDS.reviews;
        updateFilters({ kind: tab.dataset.kind, status: kind.defaultStatus, page: 1 });
      });
    }
    const engagementStatusTabs = $("#engagementStatusTabs");
    if (engagementStatusTabs) {
      engagementStatusTabs.addEventListener("click", (e) => {
        const tab = e.target.closest(".tab");
        if (!tab) return;
        updateFilters({ status: tab.dataset.status, page: 1 });
      });
    }
    const engagementKw = $("#engagementKeyword");
    if (engagementKw) {
      engagementKw.addEventListener(
        "input",
        debounce((e) => updateFilters({ keyword: e.target.value.trim(), page: 1 }), 300)
      );
    }
    const engagementResetBtn = $("#engagementReset");
    if (engagementResetBtn) engagementResetBtn.addEventListener("click", resetEngagement);

    /* ---- 第 11 批：滞纳金 / 赔偿 / 盘点 / 采编 / 批量导入 / 催还与打印 ---- */

    const on = (sel, evt, fn) => {
      const node = $(sel);
      if (node) node.addEventListener(evt, fn);
    };
    const onTabs = (sel, patchOf) =>
      on(sel, "click", (e) => {
        const tab = e.target.closest(".tab");
        if (!tab) return;
        const patch = patchOf(tab);
        if (patch) updateFilters(patch);
      });

    // 滞纳金管理
    onTabs("#fineTabs", (tab) => ({ kind: tab.dataset.kind || "", page: 1 }));
    on("#fineKeyword", "input", debounce((e) => updateFilters({ keyword: e.target.value.trim(), page: 1 }), 300));
    on("#fineReset", "click", resetFines);
    on("#fineExport", "click", exportFines);
    on("#fineCollect", "click", openFineCollectDialog);

    // 赔偿处理
    onTabs("#compensationTabs", (tab) => ({ status: tab.dataset.status, page: 1, kind: state.compensations.kind }));
    on("#compensationKind", "change", () => updateFilters({ kind: $("#compensationKind").value, page: 1 }));
    on("#compensationKeyword", "input", debounce((e) => updateFilters({ keyword: e.target.value.trim(), page: 1 }), 300));
    on("#compensationReset", "click", resetCompensations);
    on("#compensationExport", "click", exportCompensations);
    on("#compensationCreate", "click", openCompensationForm);

    // 盘点
    onTabs("#stocktakingTabs", (tab) => ({ status: tab.dataset.status || "", page: 1 }));
    on("#stocktakingLocation", "change", () => updateFilters({ location: $("#stocktakingLocation").value, page: 1 }));
    on("#stocktakingKeyword", "input", debounce((e) => updateFilters({ keyword: e.target.value.trim(), page: 1 }), 300));
    on("#stocktakingReset", "click", resetStocktaking);
    on("#stocktakingCreate", "click", openStocktakingCreate);

    // 采编验收
    onTabs("#acquisitionTabs", (tab) => ({ status: tab.dataset.status, page: 1 }));
    on("#acquisitionKeyword", "input", debounce((e) => updateFilters({ keyword: e.target.value.trim(), page: 1 }), 300));
    on("#acquisitionReset", "click", resetAcquisitions);
    on("#acquisitionExport", "click", exportAcquisitions);
    on("#acquisitionCreate", "click", openAcquisitionForm);

    // 批量导入
    on("#importKindTabs", "click", (e) => {
      const tab = e.target.closest(".tab");
      if (!tab || tab.dataset.kind === state.imports.kind) return;
      state.imports.kind = tab.dataset.kind;
      state.imports.xlsx = "";
      state.imports.fileName = "";
      const paste = $("#importPaste");
      if (paste) paste.value = "";
      const wrap = $("#importResultWrap");
      if (wrap) wrap.hidden = true;
      syncToolbar("imports");
      syncUrl();
      refreshImportTip();
    });
    on("#importTemplate", "click", downloadImportTemplate);
    on("#importPreview", "click", () => runImport(true).catch(showError));
    on("#importRun", "click", () => runImport(false).catch(showError));
    on("#importClear", "click", () => {
      $("#importPaste").value = "";
      state.imports.xlsx = "";
      state.imports.fileName = "";
      const wrap = $("#importResultWrap");
      if (wrap) wrap.hidden = true;
      refreshImportTip();
    });
    on("#importPaste", "input", () => {
      // 手工改动粘贴框即视为改用文本来源，丢掉之前载入的 Excel，避免"看着是文本、其实提交 Excel"
      if (state.imports.xlsx) {
        state.imports.xlsx = "";
        state.imports.fileName = "";
      }
    });
    on("#importFile", "change", (e) => {
      const file = e.target.files && e.target.files[0];
      if (file) pickImportFile(file);
      e.target.value = "";
    });

    // 审批中心
    onTabs("#approvalTabs", (tab) => ({ status: tab.dataset.status || "", page: 1 }));
    on("#approvalKeyword", "input", debounce((e) => updateFilters({ keyword: e.target.value.trim(), page: 1 }), 300));
    on("#approvalReset", "click", resetApprovals);

    // 系统维护
    onTabs("#systemTabs", (tab) => ({ pane: tab.dataset.pane }));
    on("#systemRefresh", "click", () => loadSystem().catch(showError));
    on("#jobRunNow", "click", runJobNow);
    on("#mailTest", "click", sendTestMail);
    on("#mailRetry", "click", retryFailedMail);
    on("#backupCreate", "click", createBackup);

    // 批量催还 / 打印
    on("#loanNotifyOverdue", "click", notifyOverdue);
    on("#loanPrintOverdue", "click", printOverdueSlips);

    // 六个视图补齐导出
    on("#reservationExport", "click", exportReservations);
    on("#logExport", "click", exportLogs);
    on("#announcementExport", "click", exportAnnouncements);
    on("#applicationExport", "click", exportApplications);
    on("#engagementExport", "click", exportEngagementRows);

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
