/* 读者个人中心 */
(function () {
  "use strict";

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  const esc = (value) =>
    String(value == null ? "" : value).replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    }[ch]));

  const fmtDate = (value) => (value ? String(value).slice(0, 10) : "—");
  const money = (value) => `¥${Number(value || 0).toFixed(2)}`;

  let csrfToken = "";
  const state = {
    status: "all",
    page: 1,
    pageSize: 10,
    member: null,
    rules: null,
    resSummary: null,
    fineSummary: null,
    reservations: null,
    fines: null,
  };

  class ApiError extends Error {
    constructor(message, status, code, field) {
      super(message);
      this.status = status;
      this.code = code;
      this.field = field;
    }
  }

  const api = {
    async request(path, options) {
      const opts = options || {};
      const init = { method: opts.method || "GET", headers: { Accept: "application/json" } };
      if (opts.body !== undefined) {
        init.headers["Content-Type"] = "application/json";
        init.body = JSON.stringify(opts.body);
      }
      if (opts.method && opts.method !== "GET" && csrfToken) {
        init.headers["X-CSRF-Token"] = csrfToken;
      }

      let response;
      try {
        response = await fetch(path, init);
      } catch (err) {
        throw new ApiError("无法连接服务器，请确认服务是否在运行", 0, "network_error");
      }

      if (response.status === 401) {
        location.replace("/?next=" + encodeURIComponent(location.pathname));
        throw new ApiError("登录状态已失效", 401, "unauthorized");
      }

      const text = await response.text();
      let payload = null;
      if (text) {
        try {
          payload = JSON.parse(text);
        } catch (err) {
          payload = null;
        }
      }
      if (!response.ok) {
        const errObj = (payload && payload.error) || {};
        throw new ApiError(
          errObj.message || `请求失败（HTTP ${response.status}）`,
          response.status,
          errObj.code || "http_error",
          errObj.details && errObj.details.field
        );
      }
      return payload;
    },
    get(path) {
      return this.request(path, { method: "GET" });
    },
    post(path, body) {
      return this.request(path, { method: "POST", body: body === undefined ? {} : body });
    },
    put(path, body) {
      return this.request(path, { method: "PUT", body: body === undefined ? {} : body });
    },
  };

  /* ---------------- 提示 ---------------- */

  function toast(title, body, kind, action) {
    const layer = $("#toastLayer");
    const node = document.createElement("div");
    node.className = "toast " + (kind || "");
    node.setAttribute("role", kind === "error" ? "alert" : "status");
    node.innerHTML =
      '<button class="t-close" type="button" aria-label="关闭提示">✕</button>' +
      `<div class="t-title">${esc(title)}</div>` +
      (body ? `<div class="t-body">${esc(body)}</div>` : "");
    layer.appendChild(node);

    if (action && action.label && typeof action.onClick === "function") {
      const btn = document.createElement("button");
      btn.className = "btn sm t-action";
      btn.type = "button";
      btn.textContent = action.label;
      btn.addEventListener("click", () => { action.onClick(); node.remove(); });
      node.appendChild(btn);
    }

    const wait = kind === "error" ? 6000 : 3600;
    let timer = setTimeout(dismiss, wait);
    function dismiss() {
      clearTimeout(timer);
      node.classList.add("out");
      setTimeout(() => node.remove(), 220);
    }
    node.querySelector(".t-close").addEventListener("click", dismiss);
    // 悬停暂停自动关闭
    node.addEventListener("mouseenter", () => clearTimeout(timer));
    node.addEventListener("mouseleave", () => { timer = setTimeout(dismiss, wait); });
  }

  function showError(err) {
    if (err && err.code === "unauthorized") return;
    if (err && err.code === "csrf_failed") {
      toast("页面会话已过期", "继续重试不会成功，请刷新页面后再操作。", "error", {
        label: "立即刷新",
        onClick: () => location.reload(),
      });
      return;
    }
    toast("操作失败", (err && err.message) || "未知错误", "error");
  }

  /* ---------------- 渲染 ---------------- */

  function renderHeader(member, stats) {
    $("#headName").textContent = member.name;
    $("#headCard").textContent = member.card_no;
    $("#welcomeName").textContent = member.name;
    $("#quotaLeft").textContent = stats.quota_left;

    const lastLogin = member.last_login_at ? String(member.last_login_at).slice(0, 16) : "首次登录";
    $("#welcomeMeta").textContent = `${member.card_no} · ${member.member_type}读者 · 可借上限 ${member.max_borrow} 册 · 上次登录 ${lastLogin}`;

    const chips = [`<span class="chip">在借 ${stats.active_loans} 册</span>`];
    if (stats.overdue_loans > 0) {
      chips.push(`<span class="chip is-warn">逾期 ${stats.overdue_loans} 册 · 请联系馆员处理</span>`);
    } else {
      chips.push('<span class="chip">暂无逾期</span>');
    }
    if (member.status !== "active") {
      chips.push('<span class="chip is-warn">账号已冻结，暂不可借阅</span>');
    }

    // 借书证挂失：横幅 + 挂失/解挂按钮互斥显示
    const lost = !!member.card_lost;
    $("#lostBanner").hidden = !lost;
    $("#lostAction").hidden = lost;
    if (lost) {
      chips.push('<span class="chip is-warn">借书证已挂失</span>');
      $("#lostHint").textContent = member.lost_at
        ? `挂失时间 ${String(member.lost_at).slice(0, 16)}；挂失期间无法借书、续借与预约。补办新证或确认证件已找回后，可解除挂失。`
        : "挂失期间无法借书、续借与预约。补办新证或确认证件已找回后，可解除挂失。";
    }
    $("#heroChips").innerHTML = chips.join("");
  }

  function renderStats(stats) {
    const res = state.resSummary || { open_total: 0, ready_total: 0 };
    const fine = state.fineSummary || { total_outstanding: 0 };
    const cards = [
      { label: "当前在借", value: stats.active_loans, note: "册", cls: "" },
      {
        label: "逾期未还",
        value: stats.overdue_loans,
        note: stats.overdue_loans > 0 ? "请尽快归还" : "状态良好",
        cls: stats.overdue_loans > 0 ? "is-danger" : "is-ok",
      },
      { label: "累计借阅", value: stats.loan_total, note: `已归还 ${stats.returned_total} 册`, cls: "" },
      { label: "剩余可借", value: stats.quota_left, note: `上限 ${state.member ? state.member.max_borrow : "—"} 册`, cls: "" },
      {
        label: "预约中",
        value: res.open_total,
        note: res.ready_total > 0 ? `${res.ready_total} 条已到书，请到馆借阅` : "暂无可取预约",
        cls: res.ready_total > 0 ? "is-ok" : "",
      },
      {
        label: "待缴滞纳金",
        value: money(fine.total_outstanding),
        note: fine.total_outstanding > 0 ? "可在线自助缴纳" : "无待缴",
        cls: fine.total_outstanding > 0 ? "is-warn" : "is-ok",
      },
      {
        label: "累计滞纳金",
        value: money(stats.fine_total),
        note: stats.overdue_days_total ? `逾期共 ${stats.overdue_days_total} 天` : "无逾期记录",
        cls: stats.fine_total > 0 ? "is-warn" : "is-ok",
      },
    ];
    $("#statGrid").innerHTML = cards
      .map(
        (c) => `<div class="stat ${c.cls}">
          <div class="s-label">${esc(c.label)}</div>
          <div class="s-value">${esc(c.value)}</div>
          <div class="s-note">${esc(c.note)}</div>
        </div>`
      )
      .join("");
  }

  function renderProfile(member) {
    $("#pCardNo").value = member.card_no;
    $("#pName").value = member.name;
    $("#pType").value = member.member_type;
    $("#pStatus").value = member.status === "active" ? "正常" : "已冻结";
    $("#pPhone").value = member.phone || "";
    $("#pEmail").value = member.email || "";
  }

  function renderRules(rules) {
    const cap = Number(rules.fine_cap) || 0;
    const capNote = cap > 0 ? `（单笔封顶 <b>${money(cap)}</b>）` : "";
    $("#rulesNote").innerHTML =
      `借期 <b>${rules.loan_days}</b> 天 · 最多续借 <b>${rules.max_renew}</b> 次 · ` +
      `逾期滞纳金 <b>${money(rules.fine_per_day)}</b> / 天${capNote}。逾期期间无法续借，请先归还。`;
  }

  function statusBadge(loan) {
    if (loan.status === "returned") return '<span class="badge badge-muted">已归还</span>';
    if (loan.status === "overdue") {
      return `<span class="badge badge-danger">逾期 ${loan.overdue_days} 天</span>`;
    }
    return '<span class="badge badge-info">在借</span>';
  }

  function renderLoans(payload) {
    const body = $("#loanBody");
    if (!payload.items.length) {
      body.innerHTML = `<tr><td colspan="6"><div class="empty-state">
        <div class="empty-ico">▤</div><p>暂无借阅记录</p>
        <small>到馆办理借书后，这里会显示你正在借阅的图书</small>
      </div></td></tr>`;
    } else {
      body.innerHTML = payload.items
        .map((loan) => {
          const renewNote = Number(loan.renew_count) > 0 ? `<div class="loan-sub">已续借 ${loan.renew_count} 次</div>` : "";
          const dateCell =
            loan.status === "returned"
              ? fmtDate(loan.returned_at)
              : `${fmtDate(loan.due_at)}${loan.overdue_days ? `<div class="loan-sub">超期 ${loan.overdue_days} 天</div>` : ""}`;
          const action = loan.can_renew
            ? `<button class="btn btn-outline btn-mini" data-renew="${loan.id}">续借</button>`
            : loan.status === "returned"
              ? '<span class="loan-sub">—</span>'
              : '<span class="loan-sub">不可续借</span>';
          return `<tr>
            <td>
              <div class="loan-title">《${esc(loan.book_title)}》</div>
              <div class="loan-sub">${esc(loan.book_author || "佚名")} · ${esc(loan.book_isbn)}</div>
              <div class="loan-sub loan-code">条码 ${esc(loan.book_barcode || "—")}</div>
            </td>
            <td class="mono">${fmtDate(loan.borrowed_at)}</td>
            <td class="mono">${dateCell}</td>
            <td>${statusBadge(loan)}${renewNote}</td>
            <td class="mono">${loan.fine > 0 ? `<span class="fine-tag">${money(loan.fine)}</span>` : "—"}</td>
            <td><div class="row-actions">${action}</div></td>
          </tr>`;
        })
        .join("");
    }

    $("#loanSummary").textContent = `共 ${payload.total} 条记录`;
    renderPager(payload);
  }

  function renderPager(payload) {
    const pager = $("#loanPager");
    pager.innerHTML = `
      <span class="page-info">第 ${payload.page} / ${payload.pages} 页 · 共 ${payload.total} 条</span>
      <button type="button" data-page="${payload.page - 1}" ${payload.page <= 1 ? "disabled" : ""}>上一页</button>
      <button type="button" data-page="${payload.page + 1}" ${payload.page >= payload.pages ? "disabled" : ""}>下一页</button>`;
  }

  /* ---------------- 预约与滞纳金渲染 ---------------- */

  function reservationBadge(r) {
    if (r.status === "ready") return '<span class="badge badge-ok">已到书</span>';
    if (r.status === "waiting") return '<span class="badge badge-info">排队中</span>';
    if (r.status === "fulfilled") return '<span class="badge badge-muted">已借出</span>';
    return `<span class="badge badge-muted">${esc(r.status_label || r.status)}</span>`;
  }

  function renderReservations(payload) {
    const body = $("#reservationBody");
    const items = payload.items || [];
    if (!items.length) {
      body.innerHTML = `<tr><td colspan="4"><div class="empty-state">
        <div class="empty-ico">◷</div><p>暂无预约</p>
        <small>在右侧搜索「已借完」的图书即可排队预约</small>
      </div></td></tr>`;
    } else {
      body.innerHTML = items
        .map((r) => {
          const pos =
            r.status === "waiting" && r.queue_position
              ? `<div class="loan-sub">排在第 ${r.queue_position} 位</div>`
              : "";
          const readyNote = r.status === "ready" ? '<div class="loan-sub">请到馆办理借出</div>' : "";
          const action = r.can_cancel
            ? `<button class="btn btn-outline btn-mini" data-cancel-res="${r.id}">取消</button>`
            : '<span class="loan-sub">—</span>';
          return `<tr>
            <td>
              <div class="loan-title">《${esc(r.book_title)}》</div>
              <div class="loan-sub">${esc(r.book_author || "佚名")} · ${esc(r.book_isbn || "")}</div>
            </td>
            <td class="mono">${fmtDate(r.created_at)}</td>
            <td>${reservationBadge(r)}${pos}${readyNote}</td>
            <td><div class="row-actions">${action}</div></td>
          </tr>`;
        })
        .join("");
    }
    $("#reservationSummary").textContent = `共 ${payload.total} 条 · 进行中 ${payload.open_total || 0} 条`;
  }

  function renderReserveResults(payload) {
    const box = $("#reserveResults");
    const items = payload.items || [];
    if (!items.length) {
      box.innerHTML = '<div class="reserve-hint">没有找到匹配的图书，换个关键词试试。</div>';
      return;
    }
    box.innerHTML = items
      .map((b) => {
        const total = Number(b.total_copies) || 0;
        const avail = Number(b.available_copies) || 0;
        const reservable = total > 0 && avail === 0;
        const state =
          total <= 0
            ? '<span class="badge badge-muted">已下架</span>'
            : avail > 0
              ? `<span class="badge badge-ok">可借 ${avail} 册</span>`
              : '<span class="badge badge-warn">已借完</span>';
        const action = reservable
          ? `<button class="btn btn-outline btn-mini" data-reserve="${b.id}">预约</button>`
          : `<button class="btn btn-outline btn-mini" disabled title="${avail > 0 ? "有可借复本，可直接借阅" : "不可预约"}">预约</button>`;
        return `<div class="reserve-item${reservable ? "" : " is-unavailable"}">
          <div class="ri-main">
            <div class="ri-title">《${esc(b.title)}》</div>
            <div class="ri-sub">${esc(b.author || "佚名")} · ${esc(b.category || "未分类")}${b.location ? " · " + esc(b.location) : ""}</div>
          </div>
          <div class="ri-state">${state}</div>
          ${action}
        </div>`;
      })
      .join("");
  }

  function renderFineHistory(rows) {
    const box = $("#fineHistory");
    if (!rows.length) {
      box.innerHTML = '<div class="fh-empty">暂无缴纳记录</div>';
      return;
    }
    box.innerHTML =
      '<div class="fh-title">缴纳记录</div>' +
      rows
        .map(
          (h) => `<div class="fh-row">
            <span>${esc(String(h.created_at || "").slice(0, 16))}</span>
            <span>${esc(h.kind_label || "")}</span>
            <span>《${esc(h.book_title || "")}》</span>
            <span class="fh-amt">${money(h.amount)}</span>
            <span>${esc(h.method_label || "")}</span>
          </div>`
        )
        .join("");
  }

  function renderFines(payload) {
    const body = $("#fineBody");
    const items = payload.items || [];
    if (!items.length) {
      body.innerHTML = `<tr><td colspan="6"><div class="empty-state">
        <div class="empty-ico">¥</div><p>暂无待缴滞纳金</p>
        <small>按时归还就不会产生滞纳金</small>
      </div></td></tr>`;
    } else {
      body.innerHTML = items
        .map((f) => {
          const capNote = f.capped ? '<div class="loan-sub">已封顶</div>' : "";
          return `<tr>
            <td>
              <div class="loan-title">《${esc(f.book_title)}》</div>
              <div class="loan-sub">${esc(f.state_label)} · 应还 ${fmtDate(f.due_at)}</div>
            </td>
            <td class="mono">${f.overdue_days} 天</td>
            <td class="amount">${money(f.fine)}${capNote}</td>
            <td class="amount">${money(f.settled)}</td>
            <td class="amount"><span class="fine-tag">${money(f.outstanding)}</span></td>
            <td><div class="row-actions"><button class="btn btn-primary btn-mini" data-pay="${f.loan_id}">缴纳</button></div></td>
          </tr>`;
        })
        .join("");
    }
    $("#fineSummary").textContent = `待缴合计 ${money(payload.total_outstanding)} · 累计已缴 ${money(payload.total_paid)}`;
    $("#payAllBtn").hidden = !(payload.total_outstanding > 0);
    renderFineHistory(payload.history || []);
    renderFineNote(payload.rules);
  }

  function renderFineNote(rules) {
    if (!rules) return;
    const cap = Number(rules.fine_cap) || 0;
    const capNote = cap > 0 ? `，单笔封顶 <b>${money(cap)}</b>` : "";
    $("#fineNote").innerHTML =
      `滞纳金按逾期整日计算：每册每天 <b>${money(rules.fine_per_day)}</b>${capNote}。` +
      `在借逾期会持续累加，建议尽快归还。在线缴纳为自助登记，缴费后如需凭证请联系馆员。`;
  }

  /* ---------------- 数据加载 ---------------- */

  async function loadMe() {
    const payload = await api.get("/api/reader/me");
    csrfToken = payload.csrf_token || csrfToken;
    state.member = payload.member;
    state.rules = payload.rules;
    state.resSummary = payload.reservations;
    state.fineSummary = payload.fines;
    renderHeader(payload.member, payload.stats);
    renderStats(payload.stats);
    renderProfile(payload.member);
    renderRules(payload.rules);
  }

  async function loadLoans() {
    const params = new URLSearchParams({ status: state.status, page: state.page, page_size: state.pageSize });
    renderLoans(await api.get("/api/reader/loans?" + params.toString()));
  }

  async function loadReservations() {
    const payload = await api.get("/api/reader/reservations?status=all&page=1&page_size=50");
    state.reservations = payload;
    renderReservations(payload);
  }

  async function loadFines() {
    const payload = await api.get("/api/reader/fines");
    state.fines = payload;
    renderFines(payload);
  }

  async function refresh() {
    await Promise.all([loadMe(), loadLoans(), loadReservations(), loadFines()]);
  }

  /* ---------------- 交互 ---------------- */

  function bindEvents() {
    $("#loanTabs").addEventListener("click", (e) => {
      const btn = e.target.closest(".tab");
      if (!btn) return;
      $$(".tab", $("#loanTabs")).forEach((t) => t.classList.toggle("is-active", t === btn));
      state.status = btn.dataset.status;
      state.page = 1;
      loadLoans().catch(showError);
    });

    $("#loanPager").addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-page]");
      if (!btn || btn.disabled) return;
      state.page = Number(btn.dataset.page);
      loadLoans().catch(showError);
    });

    $("#loanBody").addEventListener("click", async (e) => {
      const btn = e.target.closest("button[data-renew]");
      if (!btn) return;
      btn.disabled = true;
      btn.textContent = "处理中…";
      try {
        const result = await api.post(`/api/reader/loans/${btn.dataset.renew}/renew`);
        toast("续借成功", `应还日期已顺延至 ${fmtDate(result.due_at)}（第 ${result.renew_count} 次续借）`, "ok");
        await refresh();
      } catch (err) {
        showError(err);
        btn.disabled = false;
        btn.textContent = "续借";
      }
    });

    $("#profileForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      clearFieldErrors();
      const btn = $("#profileBtn");
      btn.disabled = true;
      btn.textContent = "保存中…";
      try {
        const member = await api.put("/api/reader/profile", {
          phone: $("#pPhone").value.trim(),
          email: $("#pEmail").value.trim(),
        });
        state.member = member;
        renderProfile(member);
        toast("已保存", "联系方式已更新", "ok");
      } catch (err) {
        if (err.field === "phone") markFieldError("fieldPhone", err.message);
        else if (err.field === "email") markFieldError("fieldEmail", err.message);
        else showError(err);
      } finally {
        btn.disabled = false;
        btn.textContent = "保存联系方式";
      }
    });

    $("#pwForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      clearFieldErrors();
      const oldPassword = $("#oldPassword").value;
      const newPassword = $("#newPassword").value;
      const confirm = $("#newPassword2").value;

      if (!oldPassword) return markFieldError("fieldOld", "请输入当前密码");
      if (newPassword.length < 6) return markFieldError("fieldNew", "新密码至少 6 位");
      if (newPassword !== confirm) return markFieldError("fieldNew2", "两次输入的新密码不一致");

      const btn = $("#pwBtn");
      btn.disabled = true;
      btn.textContent = "提交中…";
      try {
        await api.post("/api/reader/password", { old_password: oldPassword, new_password: newPassword });
        toast("密码已修改", "请使用新密码重新登录", "ok");
        csrfToken = "";
        setTimeout(() => location.replace("/"), 900);
      } catch (err) {
        if (err.field === "old_password") markFieldError("fieldOld", err.message);
        else if (err.field === "new_password") markFieldError("fieldNew", err.message);
        else showError(err);
        btn.disabled = false;
        btn.textContent = "修改密码";
      }
    });

    $("#logoutBtn").addEventListener("click", async () => {
      try {
        await api.post("/api/reader/logout");
      } catch (err) {
        /* 登录态已失效也直接回首页 */
      }
      location.replace("/");
    });

    /* ---- 预约 ---- */
    $("#reserveSearchForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const box = $("#reserveResults");
      const btn = $("#reserveSearchBtn");
      box.innerHTML = '<div class="reserve-hint">搜索中…</div>';
      btn.disabled = true;
      try {
        const keyword = $("#reserveKeyword").value.trim();
        const payload = await api.get(
          "/api/reader/books/search?page_size=8&keyword=" + encodeURIComponent(keyword)
        );
        renderReserveResults(payload);
      } catch (err) {
        showError(err);
        box.innerHTML = '<div class="reserve-hint">搜索失败，请稍后重试。</div>';
      } finally {
        btn.disabled = false;
      }
    });

    $("#reserveResults").addEventListener("click", async (e) => {
      const btn = e.target.closest("button[data-reserve]");
      if (!btn) return;
      btn.disabled = true;
      btn.textContent = "预约中…";
      try {
        const res = await api.post("/api/reader/reservations", { book_id: Number(btn.dataset.reserve) });
        toast("预约成功", `《${res.book_title}》已进入预约队列，到书后会在此提示`, "ok");
        btn.textContent = "已预约";
        await Promise.all([loadReservations(), loadMe()]);
      } catch (err) {
        showError(err);
        btn.disabled = false;
        btn.textContent = "预约";
      }
    });

    $("#reservationBody").addEventListener("click", async (e) => {
      const btn = e.target.closest("button[data-cancel-res]");
      if (!btn) return;
      if (!confirm("确定取消这条预约吗？取消后需重新排队。")) return;
      btn.disabled = true;
      btn.textContent = "取消中…";
      try {
        await api.post(`/api/reader/reservations/${btn.dataset.cancelRes}/cancel`);
        toast("已取消预约", "该书已从你的预约队列移除", "ok");
        await Promise.all([loadReservations(), loadMe()]);
      } catch (err) {
        showError(err);
        btn.disabled = false;
        btn.textContent = "取消";
      }
    });

    /* ---- 滞纳金 ---- */
    $("#fineBody").addEventListener("click", async (e) => {
      const btn = e.target.closest("button[data-pay]");
      if (!btn) return;
      btn.disabled = true;
      btn.textContent = "处理中…";
      try {
        const res = await api.post("/api/reader/fines/pay", { loan_id: Number(btn.dataset.pay) });
        toast("缴纳成功", res.message || "滞纳金已缴纳", "ok");
        await Promise.all([loadFines(), loadMe()]);
      } catch (err) {
        showError(err);
        btn.disabled = false;
        btn.textContent = "缴纳";
      }
    });

    $("#payAllBtn").addEventListener("click", async () => {
      const btn = $("#payAllBtn");
      if (!confirm("确定一次结清全部待缴滞纳金吗？")) return;
      btn.disabled = true;
      btn.textContent = "处理中…";
      try {
        const res = await api.post("/api/reader/fines/pay", { all: true });
        toast("已结清", res.message || "全部滞纳金已缴纳", "ok");
        await Promise.all([loadFines(), loadMe()]);
      } catch (err) {
        showError(err);
      } finally {
        btn.disabled = false;
        btn.textContent = "一次结清";
      }
    });

    /* ---- 借书证挂失 ---- */
    $("#lostBtn").addEventListener("click", () => setCardLost(true));
    $("#unlostBtn").addEventListener("click", () => setCardLost(false));
  }

  async function setCardLost(lost) {
    const message = lost
      ? "挂失后无法借书、续借与预约（已借图书与历史记录不受影响）。确定挂失这张借书证吗？"
      : "确认证件已找回或已补办？解除挂失后将恢复借阅功能。";
    if (!confirm(message)) return;
    try {
      await api.post(lost ? "/api/reader/card/lost" : "/api/reader/card/unlost");
      toast(
        lost ? "已挂失" : "已解除挂失",
        lost ? "挂失期间无法借书、续借与预约" : "借阅功能已恢复",
        "ok"
      );
      await refresh();
    } catch (err) {
      showError(err);
    }
  }

  function clearFieldErrors() {
    ["fieldPhone", "fieldEmail", "fieldOld", "fieldNew", "fieldNew2"].forEach((id) => {
      const field = document.getElementById(id);
      field.classList.remove("has-error");
      const tip = field.querySelector(".field-error");
      if (tip) {
        tip.hidden = true;
        tip.textContent = "";
      }
    });
  }

  function markFieldError(fieldId, message) {
    const field = document.getElementById(fieldId);
    field.classList.add("has-error");
    const tip = field.querySelector(".field-error");
    if (tip) {
      tip.hidden = false;
      tip.textContent = message;
    }
    const input = field.querySelector("input");
    if (input) input.focus();
  }

  document.addEventListener("DOMContentLoaded", () => {
    bindEvents();
    refresh().catch((err) => {
      if (err && err.code === "unauthorized") return;
      showError(err);
    });
  });
})();
