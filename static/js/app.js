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
    el.innerHTML = `<div class="t-title">${esc(title)}</div>` + (body ? `<div class="t-body">${esc(body)}</div>` : "");
    layer.appendChild(el);
    setTimeout(() => {
      el.style.transition = "opacity .2s, transform .2s";
      el.style.opacity = "0";
      el.style.transform = "translateX(16px)";
      setTimeout(() => el.remove(), 220);
    }, kind === "error" ? 5200 : 3000);
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

  const auth = { csrf: null, admin: null, redirecting: false };

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
      const text = await response.text();
      let payload = null;
      if (text) {
        try { payload = JSON.parse(text); } catch (err) { payload = null; }
      }
      if (!response.ok) {
        const errObj = (payload && payload.error) || {};
        if (response.status === 401) handleUnauthorized();
        throw new ApiError(
          errObj.message || `请求失败（HTTP ${response.status}）`,
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

  /* ---------------- 视图元数据 ---------------- */

  const VIEWS = {
    dashboard: { title: "概览", sub: "馆藏与流通实时概况" },
    books: { title: "图书管理", sub: "维护馆藏书目与库存" },
    members: { title: "读者管理", sub: "维护读者档案与借阅权限" },
    loans: { title: "借阅管理", sub: "办理借书、还书与续借" },
  };

  const state = {
    view: "dashboard",
    books: { page: 1, pageSize: 10, keyword: "", category: "", availableOnly: false },
    members: { page: 1, pageSize: 10, keyword: "", status: "" },
    loans: { page: 1, pageSize: 10, keyword: "", status: "all" },
    categories: [],
  };

  /* ---------------- 渲染：概览 ---------------- */

  function statCard(label, value, foot, tone) {
    return `<div class="stat ${tone || ""}">
      <div class="label">${esc(label)}</div>
      <div class="value">${esc(value)}</div>
      <div class="foot">${esc(foot || "")}</div>
    </div>`;
  }

  function renderDashboard(data) {
    const b = data.books || {};
    const m = data.members || {};
    const l = data.loans || {};

    $("#statGrid").innerHTML = [
      statCard("馆藏书目", b.total_titles || 0, `共 ${b.total_copies || 0} 册`, ""),
      statCard("可借册数", b.available_copies || 0, `${b.borrowed_copies || 0} 册在外`, "is-ok"),
      statCard("在借未还", l.open_loans || 0, `今日借出 ${l.borrowed_today || 0} 册`, ""),
      statCard("逾期未还", l.overdue_loans || 0, `预估罚金 ${money(data.fine_estimate)}`, (l.overdue_loans || 0) > 0 ? "is-danger" : "is-ok"),
      statCard("7 天内到期", l.due_soon_loans || 0, "建议提前提醒", (l.due_soon_loans || 0) > 0 ? "is-warn" : ""),
      statCard("注册读者", m.total_members || 0, `正常 ${m.active_members || 0} · 冻结 ${m.frozen_members || 0}`, ""),
    ].join("");

    const policy = data.policy || {};
    $("#policyDays").textContent = (policy.loan_days || 0) + " 天";
    $("#policyRenew").textContent = (policy.max_renew || 0) + " 次";
    $("#policyFine").textContent = money(policy.fine_per_day) + "/天";

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

  async function loadBooks() {
    const s = state.books;
    const payload = await api.get("/api/books", {
      keyword: s.keyword,
      category: s.category,
      available_only: s.availableOnly ? "1" : "",
      page: s.page,
      page_size: s.pageSize,
    });
    const table = $("#bookTable");
    if (!payload.items.length) {
      table.innerHTML = `<tbody><tr><td class="empty">没有匹配的图书</td></tr></tbody>`;
    } else {
      table.innerHTML = `
        <thead><tr>
          <th>书名 / 作者 / 简介</th><th>条码 / ISBN</th><th>分类</th><th>出版社</th><th>年份</th><th>位置</th><th>库存</th><th>操作</th>
        </tr></thead>
        <tbody>${payload.items
          .map((b) => {
            const avail = Number(b.available_copies) || 0;
            const total = Number(b.total_copies) || 0;
            const tone = avail === 0 ? "danger" : avail <= 1 ? "warn" : "ok";
            return `<tr>
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
              <td><span class="badge ${tone}">${avail} / ${total}</span></td>
              <td><div class="actions">
                <button class="btn sm" data-detail="${b.id}">详情</button>
                <button class="btn sm" data-edit="${b.id}">编辑</button>
                <button class="btn sm" data-borrow="${b.id}" ${avail === 0 ? "disabled" : ""}>借出</button>
                <button class="btn sm danger" data-del="${b.id}">删除</button>
              </div></td>
            </tr>`;
          })
          .join("")}</tbody>`;
    }

    renderPager($("#bookPager"), payload, (patch) => {
      if (patch.page !== undefined) state.books.page = patch.page;
      if (patch.pageSize !== undefined) { state.books.pageSize = patch.pageSize; state.books.page = 1; }
      loadBooks().catch(showError);
    });

    table.querySelectorAll("[data-detail]").forEach((btn) =>
      btn.addEventListener("click", () => openBookDetail(Number(btn.dataset.detail)))
    );
    table.querySelectorAll("[data-edit]").forEach((btn) =>
      btn.addEventListener("click", () => openBookForm(Number(btn.dataset.edit)))
    );
    table.querySelectorAll("[data-del]").forEach((btn) =>
      btn.addEventListener("click", () => confirmDeleteBook(Number(btn.dataset.del)))
    );
    table.querySelectorAll("[data-borrow]").forEach((btn) =>
      btn.addEventListener("click", () => openBorrowForm(Number(btn.dataset.borrow)))
    );
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
        openModal({
          title: isEdit ? `编辑图书 #${id}` : "新增图书",
          body: `
            <div class="form-grid">
              <div class="field full"><label>书名<span class="req">*</span></label><input name="title" value="${esc(b.title || "")}" placeholder="如：Python 编程：从入门到实践" /></div>
              <div class="field"><label>ISBN<span class="req">*</span></label><input name="isbn" value="${esc(b.isbn || "")}" placeholder="10 位或 13 位" /><span class="tip">可含连字符，保存时自动规整</span></div>
              <div class="field"><label>馆藏条码</label><input name="barcode" value="${esc(b.barcode || "")}" placeholder="留空则自动生成 BK 编号" /><span class="tip">4–32 位字母、数字或连字符；留空自动分配，也可填馆内已有条码</span></div>
              <div class="field"><label>作者</label><input name="author" value="${esc(b.author || "")}" /></div>
              <div class="field"><label>出版社</label><input name="publisher" value="${esc(b.publisher || "")}" /></div>
              <div class="field"><label>分类</label><input name="category" value="${esc(b.category || "")}" placeholder="未分类" /></div>
              <div class="field"><label>出版年份</label><input name="publish_year" value="${b.publish_year ? esc(b.publish_year) : ""}" placeholder="2024" /></div>
              <div class="field"><label>馆藏位置</label><input name="location" value="${esc(b.location || "")}" placeholder="A-01-03" /></div>
              <div class="field"><label>总册数<span class="req">*</span></label><input name="total_copies" value="${b.total_copies !== undefined ? esc(b.total_copies) : 1}" />
                <span class="tip">${isEdit ? `在借 ${b.active_loans || 0} 册，总册数不可小于该值` : "可借数将自动等于总册数"}</span>
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
        const tone = avail === 0 ? "danger" : avail <= 1 ? "warn" : "ok";
        const body = `
          <div class="book-detail">
            <div class="bd-head">
              <div class="bd-title">《${esc(b.title)}》</div>
              <div class="bd-sub">${esc(b.author || "佚名")} · ${esc(b.publisher || "未标注出版社")}${b.publish_year ? ` · ${esc(b.publish_year)} 年` : ""}</div>
              <div class="bd-tags">
                <span class="badge neutral">${esc(b.category)}</span>
                <span class="badge ${tone}">可借 ${avail} / ${total} 册</span>
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
            <button type="button" class="btn sm" data-edit>编辑这本图书</button>
          </div>`;
        openModal({
          title: `图书详情 #${b.id}`,
          body,
          confirmText: "关闭",
          hideCancel: true,
          onSubmit: async () => {},
          onMount: (form) => {
            const printBtn = form.querySelector("[data-print]");
            if (printBtn) printBtn.addEventListener("click", () => printBarcodeLabel(b));
            form.querySelector("[data-edit]").addEventListener("click", () => {
              closeModal();
              openBookForm(b.id);
            });
          },
        });
      })
      .catch(showError);
  }

  function printBarcodeLabel(book) {
    if (!book.barcode) {
      toast("无法打印", "该书还没有条码号，请先编辑补充", "error");
      return;
    }
    $("#printLayer").innerHTML = `
      <div class="print-label">
        <div class="pl-lib">${esc(LIBRARY_NAME)}</div>
        <div class="pl-title">《${esc(book.title)}》</div>
        <div class="pl-sub">${esc(book.author || "佚名")} · 索书号 ${esc(book.location || "待分配")}</div>
        <div class="pl-code">${Barcode.svg(book.barcode, { height: 44, fontSize: 11 })}</div>
        <div class="pl-isbn">ISBN ${esc(book.isbn)}</div>
      </div>`;
    window.print();
  }

  function confirmDeleteBook(id) {
    openModal({
      title: "删除图书",
      body: `<p>确定要删除图书 <b>#${id}</b> 吗？若该书存在借阅历史将无法删除（会提示改为下架处理）。</p>`,
      confirmText: "确认删除",
      danger: true,
      onSubmit: async () => {
        await api.del(`/api/books/${id}`);
        toast("图书已删除", `#${id}`, "ok");
        await Promise.all([loadBooks(), loadCategories()]);
      },
    });
  }

  /* ---------------- 读者视图 ---------------- */

  async function loadMembers() {
    const s = state.members;
    const payload = await api.get("/api/members", {
      keyword: s.keyword,
      status: s.status,
      page: s.page,
      page_size: s.pageSize,
    });
    const table = $("#memberTable");
    if (!payload.items.length) {
      table.innerHTML = `<tbody><tr><td class="empty">没有匹配的读者</td></tr></tbody>`;
    } else {
      table.innerHTML = `
        <thead><tr>
          <th>姓名 / 证号</th><th>类型</th><th>联系方式</th><th>可借上限</th><th>在借</th><th>逾期</th><th>状态</th><th>门户账号</th><th>操作</th>
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
                <button class="btn sm" data-edit="${m.id}">编辑</button>
                <button class="btn sm" data-history="${m.id}">借阅记录</button>
                <button class="btn sm" data-resetpw="${m.id}">重置密码</button>
                <button class="btn sm danger" data-del="${m.id}">删除</button>
              </div></td>
            </tr>`;
          })
          .join("")}</tbody>`;
    }

    renderPager($("#memberPager"), payload, (patch) => {
      if (patch.page !== undefined) state.members.page = patch.page;
      if (patch.pageSize !== undefined) { state.members.pageSize = patch.pageSize; state.members.page = 1; }
      loadMembers().catch(showError);
    });

    table.querySelectorAll("[data-edit]").forEach((btn) =>
      btn.addEventListener("click", () => openMemberForm(Number(btn.dataset.edit)))
    );
    table.querySelectorAll("[data-del]").forEach((btn) =>
      btn.addEventListener("click", () => confirmDeleteMember(Number(btn.dataset.del)))
    );
    table.querySelectorAll("[data-history]").forEach((btn) =>
      btn.addEventListener("click", () => openMemberHistory(Number(btn.dataset.history)))
    );
    table.querySelectorAll("[data-resetpw]").forEach((btn) =>
      btn.addEventListener("click", () => confirmResetPassword(Number(btn.dataset.resetpw)))
    );
  }

  function confirmResetPassword(id) {
    openModal({
      title: "重置读者门户密码",
      body: `<div class="form-note">将把该读者的登录密码重置为 <b>证号后 6 位</b>。重置后请转告读者，并提醒其登录后自行修改密码。</div>`,
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
          title: isEdit ? `编辑读者 #${id}` : "新增读者",
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

  function confirmDeleteMember(id) {
    openModal({
      title: "删除读者",
      body: `<p>确定要删除读者 <b>#${id}</b> 吗？存在未还图书或历史借阅记录的读者不可删除。</p>`,
      confirmText: "确认删除",
      danger: true,
      onSubmit: async () => {
        await api.del(`/api/members/${id}`);
        toast("读者已删除", `#${id}`, "ok");
        await loadMembers();
      },
    });
  }

  function openMemberHistory(id) {
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
        openModal({ title: `借阅记录 · 读者 #${id}`, body, confirmText: "关闭", hideCancel: true, onSubmit: async () => {} });
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
    const payload = await api.get("/api/loans", {
      keyword: s.keyword,
      status: s.status,
      page: s.page,
      page_size: s.pageSize,
    });
    const table = $("#loanTable");
    if (!payload.items.length) {
      table.innerHTML = `<tbody><tr><td class="empty">没有匹配的借阅记录</td></tr></tbody>`;
    } else {
      table.innerHTML = `
        <thead><tr>
          <th>图书</th><th>读者</th><th>借出日期</th><th>应还日期</th><th>归还日期</th><th>续借</th><th>状态</th><th>罚金</th><th>操作</th>
        </tr></thead>
        <tbody>${payload.items
          .map((r) => {
            const open = r.status !== "returned";
            return `<tr>
              <td class="wrap title-cell"><strong>${esc(r.book_title)}</strong><div class="sub">${esc(r.book_barcode || "—")} · ISBN ${esc(r.book_isbn)}</div></td>
              <td>${esc(r.member_name)}<div class="sub">${esc(r.member_card)}</div></td>
              <td class="sub">${esc(fmtDate(r.borrowed_at))}</td>
              <td class="${r.status === "overdue" ? "" : "sub"}">${esc(fmtDate(r.due_at))}</td>
              <td class="sub">${esc(fmtDate(r.returned_at))}</td>
              <td class="num">${Number(r.renew_count) || 0}</td>
              <td>${loanBadge(r)}</td>
              <td class="num">${Number(r.fine) > 0 ? money(r.fine) : '<span class="sub">—</span>'}</td>
              <td><div class="actions">
                <button class="btn sm primary" data-return="${r.id}" ${open ? "" : "disabled"}>还书</button>
                <button class="btn sm" data-renew="${r.id}" ${r.can_renew ? "" : "disabled"}>续借</button>
              </div></td>
            </tr>`;
          })
          .join("")}</tbody>`;
    }

    renderPager($("#loanPager"), payload, (patch) => {
      if (patch.page !== undefined) state.loans.page = patch.page;
      if (patch.pageSize !== undefined) { state.loans.pageSize = patch.pageSize; state.loans.page = 1; }
      loadLoans().catch(showError);
    });

    table.querySelectorAll("[data-return]").forEach((btn) =>
      btn.addEventListener("click", () => doReturn(Number(btn.dataset.return)))
    );
    table.querySelectorAll("[data-renew]").forEach((btn) =>
      btn.addEventListener("click", () => doRenew(Number(btn.dataset.renew)))
    );
  }

  function doReturn(id) {
    openModal({
      title: "办理还书",
      body: `<p>确认办理借阅记录 <b>#${id}</b> 的还书手续？系统将恢复库存并结算罚金。</p>
             <div class="form-note">未逾期不产生罚金；逾期罚金按每日标准累计。</div>`,
      confirmText: "确认还书",
      onSubmit: async () => {
        const result = await api.post(`/api/loans/${id}/return`, {});
        const extra = Number(result.fine) > 0 ? `逾期 ${result.overdue_days} 天，罚金 ${money(result.fine)}` : "按期归还，无罚金";
        toast("还书成功", `《${result.book_title}》· 条码 ${result.book_barcode || "—"} · ${extra}`, Number(result.fine) > 0 ? "warn" : "ok");
        await refreshAfterLoanChange();
      },
    });
  }

  function doRenew(id) {
    openModal({
      title: "办理续借",
      body: `<p>确认为借阅记录 <b>#${id}</b> 续借一个完整借期？</p>
             <div class="form-note">续借会在原应还日期基础上顺延；已逾期或达到续借上限的记录无法续借。</div>`,
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
          <div class="field full"><label>检索图书</label><input name="book_filter" placeholder="输入书名 / 作者 / ISBN / 条码过滤" /></div>
          <div class="field full"><label>选择图书<span class="req">*</span></label><select name="book_id" id="borrowBook"></select></div>
          <div class="field full"><label>检索读者</label><input name="member_filter" placeholder="输入姓名 / 证号过滤" /></div>
          <div class="field full"><label>选择读者<span class="req">*</span></label><select name="member_id" id="borrowMember"></select></div>
          <div class="field full"><label>备注</label><input name="note" placeholder="选填" /></div>
          <div class="form-note">列表只显示当前有可借库存的图书。有逾期未还、已达可借上限或被冻结的读者无法办理借书。</div>
        </div>`,
      confirmText: "确认借出",
      onMount: (form) => {
        const bookSel = $("#borrowBook", form);
        const memberSel = $("#borrowMember", form);

        const fillBooks = async (keyword) => {
          const payload = await api.get("/api/books", {
            keyword: keyword || "",
            available_only: "1",
            page: 1,
            page_size: 50,
          });
          const items = payload.items || [];
          bookSel.innerHTML = items.length
            ? items
                .map(
                  (b) =>
                    `<option value="${b.id}" ${prefillBookId === b.id ? "selected" : ""}>` +
                    `${esc(b.title)}（${esc(b.author || "佚名")}）· ${esc(b.barcode || "无条码")} · 可借 ${b.available_copies}/${b.total_copies}</option>`
                )
                .join("")
            : `<option value="">没有可借图书</option>`;
        };

        const fillMembers = async (keyword) => {
          const payload = await api.get("/api/members", { keyword: keyword || "", page: 1, page_size: 50 });
          const items = (payload.items || []).filter((m) => m.status === "active");
          memberSel.innerHTML = items.length
            ? items
                .map((m) => {
                  const overdue = Number(m.overdue_loans) || 0;
                  const full = Number(m.active_loans) >= Number(m.max_borrow);
                  const flag = overdue > 0 ? " · 有逾期" : full ? " · 已满额" : "";
                  return `<option value="${m.id}">${esc(m.name)}（${esc(m.card_no)}）· 在借 ${m.active_loans}/${m.max_borrow}${flag}</option>`;
                })
                .join("")
            : `<option value="">没有可借书的读者</option>`;
        };

        const onBookFilter = debounce((value) => fillBooks(value).catch(showError), 260);
        const onMemberFilter = debounce((value) => fillMembers(value).catch(showError), 260);
        form.book_filter.addEventListener("input", (e) => onBookFilter(e.target.value));
        form.member_filter.addEventListener("input", (e) => onMemberFilter(e.target.value));

        return Promise.all([fillBooks(""), fillMembers("")]);
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

  function openModal(opts) {
    const layer = $("#modalLayer");
    $("#modalTitle").textContent = opts.title || "";

    const body = $("#modalBody");
    body.innerHTML = "";
    const form = document.createElement("form");
    form.setAttribute("novalidate", "novalidate");
    form.addEventListener("submit", (e) => e.preventDefault());
    form.innerHTML = opts.body || "";
    body.appendChild(form);

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

    if (opts.onMount) {
      Promise.resolve(opts.onMount(form)).catch(showError);
    }
  }

  function closeModal() {
    $("#modalLayer").hidden = true;
    $("#modalBody").innerHTML = "";
    $("#modalFoot").innerHTML = "";
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
      }
    }
    toast("操作未完成", message, "error");
  }

  function showError(err) {
    toast("请求失败", (err && err.message) || String(err), "error");
  }

  /* ---------------- 会话 ---------------- */

  async function loadSession() {
    const data = await api.request("/api/auth/me");
    auth.admin = data.admin;
    auth.csrf = data.csrf_token;
    const name = (auth.admin && (auth.admin.display_name || auth.admin.username)) || "管理员";
    $("#adminName").textContent = name;
    $("#adminRole").textContent = auth.admin.role === "admin" ? "系统管理员" : "馆员";
    $("#adminAvatar").textContent = name.slice(0, 1);
    return data;
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

  function switchView(view) {
    if (!VIEWS[view]) view = "dashboard";
    state.view = view;
    document.querySelectorAll(".nav-item").forEach((btn) =>
      btn.classList.toggle("is-active", btn.dataset.view === view)
    );
    document.querySelectorAll(".view").forEach((sec) =>
      sec.classList.toggle("is-active", sec.id === "view-" + view)
    );
    $("#pageTitle").textContent = VIEWS[view].title;
    $("#pageSub").textContent = VIEWS[view].sub;
    location.hash = "#" + view;
    refreshCurrentView().catch(showError);
  }

  async function refreshCurrentView() {
    if (state.view === "dashboard") return loadDashboard();
    if (state.view === "books") return Promise.all([loadBooks(), loadCategories()]);
    if (state.view === "members") return loadMembers();
    if (state.view === "loans") return loadLoans();
  }

  async function loadDashboard() {
    const data = await api.get("/api/dashboard/overview");
    renderDashboard(data);
  }

  /* ---------------- 事件绑定 ---------------- */

  function bindEvents() {
    $("#nav").addEventListener("click", (e) => {
      const btn = e.target.closest(".nav-item");
      if (btn) switchView(btn.dataset.view);
    });

    document.addEventListener("click", (e) => {
      const jump = e.target.closest("[data-jump]");
      if (jump) {
        const target = jump.dataset.jump;
        if (jump.dataset.status) {
          state.loans.status = jump.dataset.status;
          document.querySelectorAll("#loanTabs .tab").forEach((t) =>
            t.classList.toggle("is-active", t.dataset.status === state.loans.status)
          );
        }
        switchView(target);
      }
    });

    // 图书
    $("#bookKeyword").addEventListener("input", debounce((e) => {
      state.books.keyword = e.target.value.trim();
      state.books.page = 1;
      loadBooks().catch(showError);
    }, 300));
    $("#bookCategory").addEventListener("change", (e) => {
      state.books.category = e.target.value;
      state.books.page = 1;
      loadBooks().catch(showError);
    });
    $("#bookAvailableOnly").addEventListener("change", (e) => {
      state.books.availableOnly = e.target.checked;
      state.books.page = 1;
      loadBooks().catch(showError);
    });
    $("#bookReset").addEventListener("click", () => {
      state.books = { page: 1, pageSize: state.books.pageSize, keyword: "", category: "", availableOnly: false };
      $("#bookKeyword").value = "";
      $("#bookCategory").value = "";
      $("#bookAvailableOnly").checked = false;
      loadBooks().catch(showError);
    });
    $("#bookCreate").addEventListener("click", () => openBookForm(null));

    // 读者
    $("#memberKeyword").addEventListener("input", debounce((e) => {
      state.members.keyword = e.target.value.trim();
      state.members.page = 1;
      loadMembers().catch(showError);
    }, 300));
    $("#memberStatus").addEventListener("change", (e) => {
      state.members.status = e.target.value;
      state.members.page = 1;
      loadMembers().catch(showError);
    });
    $("#memberReset").addEventListener("click", () => {
      state.members = { page: 1, pageSize: state.members.pageSize, keyword: "", status: "" };
      $("#memberKeyword").value = "";
      $("#memberStatus").value = "";
      loadMembers().catch(showError);
    });
    $("#memberCreate").addEventListener("click", () => openMemberForm(null));

    // 借阅
    $("#loanTabs").addEventListener("click", (e) => {
      const tab = e.target.closest(".tab");
      if (!tab) return;
      state.loans.status = tab.dataset.status;
      state.loans.page = 1;
      document.querySelectorAll("#loanTabs .tab").forEach((t) => t.classList.toggle("is-active", t === tab));
      loadLoans().catch(showError);
    });
    $("#loanKeyword").addEventListener("input", debounce((e) => {
      state.loans.keyword = e.target.value.trim();
      state.loans.page = 1;
      loadLoans().catch(showError);
    }, 300));
    $("#loanReset").addEventListener("click", () => {
      state.loans = { page: 1, pageSize: state.loans.pageSize, keyword: "", status: "all" };
      $("#loanKeyword").value = "";
      document.querySelectorAll("#loanTabs .tab").forEach((t) =>
        t.classList.toggle("is-active", t.dataset.status === "all")
      );
      loadLoans().catch(showError);
    });
    $("#loanBorrow").addEventListener("click", () => openBorrowForm(null));

    $("#modalClose").addEventListener("click", closeModal);
    $("#modalLayer").addEventListener("click", (e) => {
      if (e.target === $("#modalLayer")) closeModal();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !$("#modalLayer").hidden) closeModal();
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
  }

  /* ---------------- 启动 ---------------- */

  document.addEventListener("DOMContentLoaded", async () => {
    bindEvents();
    try {
      await loadSession();
    } catch (err) {
      return; // 401 已触发跳转
    }
    const initial = (location.hash || "").replace("#", "");
    switchView(VIEWS[initial] ? initial : "dashboard");
  });
})();
