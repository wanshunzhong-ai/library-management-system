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
  const state = { status: "all", page: 1, pageSize: 10, member: null, rules: null };

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
    $("#heroChips").innerHTML = chips.join("");
  }

  function renderStats(stats) {
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

  /* ---------------- 数据加载 ---------------- */

  async function loadMe() {
    const payload = await api.get("/api/reader/me");
    csrfToken = payload.csrf_token || csrfToken;
    state.member = payload.member;
    state.rules = payload.rules;
    renderHeader(payload.member, payload.stats);
    renderStats(payload.stats);
    renderProfile(payload.member);
    renderRules(payload.rules);
  }

  async function loadLoans() {
    const params = new URLSearchParams({ status: state.status, page: state.page, page_size: state.pageSize });
    renderLoans(await api.get("/api/reader/loans?" + params.toString()));
  }

  async function refresh() {
    await Promise.all([loadMe(), loadLoans()]);
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
