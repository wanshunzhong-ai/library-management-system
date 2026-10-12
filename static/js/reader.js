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
    // ---- 第 10 批：消息中心 / 我的书架 / 评价 / 申请 ----
    notifyKind: "all",
    notifyPage: 1,
    notifyUnread: 0,
    shelfPage: 1,
    shelfKeyword: "",
    reviewPendingTotal: 0,
    profileFields: [],
    memberTypes: [],
    gateOpen: false,
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
    del(path) {
      return this.request(path, { method: "DELETE" });
    },
  };

  /* ---------------- CSV 导出 ----------------
   * 读者页面不加载后台的 app.js，所以这套 helper 要在本文件内自备一份。
   * 与后台保持同一口径：字段转义 + \uFEFF BOM（否则 Excel 打开是乱码）+ 时间戳后缀。
   */

  function csvCell(value) {
    const text = value == null ? "" : String(value);
    return /[",\n\r]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
  }

  function fileStamp() {
    const d = new Date();
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}`;
  }

  function downloadCsv(filename, header, rows) {
    if (!rows || !rows.length) {
      toast("没有可导出的数据", "当前筛选下没有记录，换个条件再试。", "warn");
      return;
    }
    const lines = [header, ...rows].map((row) => row.map(csvCell).join(","));
    const blob = new Blob(["\uFEFF" + lines.join("\r\n")], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  /** 分页接口一次性取全（导出要的是「全部」而不是当前页）。 */
  async function collectAll(basePath, params, pageSize) {
    const size = pageSize || 100;
    const out = [];
    let page = 1;
    let pages = 1;
    do {
      const query = new URLSearchParams({ ...(params || {}), page: String(page), page_size: String(size) });
      const payload = await api.get(`${basePath}?${query.toString()}`);
      out.push(...(payload.items || []));
      pages = Number(payload.pages || 1);
      page += 1;
      if (page > 40) break; // 防御：任何情况下都不要死循环
    } while (page <= pages);
    return out;
  }

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
    const resSummary = state.resSummary || {};
    if (resSummary.ready_total > 0) {
      chips.push(`<span class="chip is-ok">${resSummary.ready_total} 本预约已到书 · 请在保留期内到馆借阅</span>`);
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
    const subscribed = $("#pEmailSubscribed");
    if (subscribed) subscribed.checked = member.email_subscribed !== false;
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

  /** 通用分页条（消息中心 / 我的书架各自一套，样式与借阅分页一致）。 */
  function renderGenericPager(selector, payload) {
    const pager = $(selector);
    if (!pager) return;
    if (!payload || !payload.total) {
      pager.innerHTML = "";
      return;
    }
    pager.innerHTML = `
      <span class="page-info">第 ${payload.page} / ${payload.pages} 页 · 共 ${payload.total} 条</span>
      <button type="button" data-page="${payload.page - 1}" ${payload.page <= 1 ? "disabled" : ""}>上一页</button>
      <button type="button" data-page="${payload.page + 1}" ${payload.page >= payload.pages ? "disabled" : ""}>下一页</button>`;
  }

  /** 星级展示：avg 为平均分，count 为评价条数；无评价时返回空串（不显示空壳星星）。 */
  function starsHtml(avg, count, size) {
    const total = Number(count) || 0;
    if (total <= 0) return "";
    const num = Number(avg) || 0;
    const cls = size === "sm" ? "stars stars-sm" : "stars";
    const marks = [1, 2, 3, 4, 5]
      .map((i) => `<i class="${num >= i - 0.5 ? "on" : ""}">★</i>`)
      .join("");
    return `<span class="${cls}" title="${num} 分（${total} 条评价）">${marks}</span>
      <span class="rating-num">${num.toFixed(1)}</span><span class="rating-cnt">（${total}）</span>`;
  }

  /* ---------------- 轻量弹窗 ----------------
   * 读者页只用到两个场景（写评价、预约改约），不值得引入完整组件库。
   * 这里做一个最小实现：遮罩 + 卡片 + Esc 关闭 + 点击遮罩关闭。
   */
  function openDialog(options) {
    const opts = options || {};
    const mask = document.createElement("div");
    mask.className = "dlg-mask";
    mask.innerHTML = `<div class="dlg" role="dialog" aria-modal="true">
        <div class="dlg-head">
          <h3>${esc(opts.title || "")}</h3>
          <button class="dlg-x" type="button" aria-label="关闭">✕</button>
        </div>
        <div class="dlg-body"></div>
        <div class="dlg-foot"></div>
      </div>`;
    mask.querySelector(".dlg-body").innerHTML = opts.body || "";
    const foot = mask.querySelector(".dlg-foot");
    if (opts.footer) foot.innerHTML = opts.footer;
    else foot.remove();

    function close() {
      document.removeEventListener("keydown", onKey);
      document.body.classList.remove("dlg-open");
      mask.classList.add("out");
      setTimeout(() => mask.remove(), 160);
      if (typeof opts.onClose === "function") opts.onClose();
    }
    function onKey(e) {
      if (e.key === "Escape") close();
    }
    mask.querySelector(".dlg-x").addEventListener("click", close);
    mask.addEventListener("click", (e) => {
      if (e.target === mask) close();
    });
    document.addEventListener("keydown", onKey);
    document.body.appendChild(mask);
    document.body.classList.add("dlg-open");

    const api2 = { root: mask, close: close };
    if (typeof opts.onMount === "function") opts.onMount(api2);
    return api2;
  }

  /** 星级选择控件：读写 value，供评价弹窗使用。 */
  function starPickerHtml(id, value) {
    const current = Number(value) || 0;
    return `<div class="star-picker" id="${id}" data-value="${current}">
      ${[1, 2, 3, 4, 5]
        .map((i) => `<button type="button" class="sp-star${current >= i ? " on" : ""}" data-star="${i}" aria-label="${i} 星">★</button>`)
        .join("")}
      <span class="sp-label">${current ? `${current} 星` : "点击选择星级"}</span>
    </div>`;
  }

  function bindStarPicker(root, onChange) {
    const picker = root.querySelector(".star-picker");
    if (!picker) return;
    picker.addEventListener("click", (e) => {
      const btn = e.target.closest(".sp-star");
      if (!btn) return;
      const value = Number(btn.dataset.star);
      picker.dataset.value = String(value);
      $$(".sp-star", picker).forEach((s) => s.classList.toggle("on", Number(s.dataset.star) <= value));
      const label = picker.querySelector(".sp-label");
      if (label) label.textContent = `${value} 星`;
      if (typeof onChange === "function") onChange(value);
    });
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
          // 已到书：把保留期倒计时摆到最显眼的位置——过期未取会被自动释放
          let readyNote = "";
          if (r.status === "ready") {
            const left = r.hold_left_days;
            const urgent = left !== null && left !== undefined && left <= 1;
            readyNote = `<div class="hold-note${urgent ? " is-urgent" : ""}">${esc(
              r.hold_label || "请尽快到馆借阅"
            )}</div>`;
          }
          const actions = [];
          if (r.can_rebook) {
            actions.push(`<button class="btn btn-outline btn-mini" data-rebook-res="${r.id}">改约</button>`);
          }
          if (r.can_cancel) {
            actions.push(`<button class="btn btn-outline btn-mini" data-cancel-res="${r.id}">取消</button>`);
          }
          const action = actions.length ? actions.join("") : '<span class="loan-sub">—</span>';
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

  /* ================================================================
   * 第 10 批：消息中心 / 我的书架 / 评价 / 荐购 / 资料变更 / 电子借书证
   * ================================================================ */

  /* ---------------- 强制改密遮罩 ---------------- */

  function showPasswordGate() {
    const gate = $("#pwGate");
    if (!gate) return;
    state.gateOpen = true;
    gate.hidden = false;
    document.body.classList.add("gate-open");
    const first = $("#gateOld");
    if (first) setTimeout(() => first.focus(), 80);
  }

  function hidePasswordGate() {
    const gate = $("#pwGate");
    if (!gate) return;
    state.gateOpen = false;
    gate.hidden = true;
    document.body.classList.remove("gate-open");
  }

  /* ---------------- 消息中心 ---------------- */

  // 通知里的 link_view 是「业务视图名」，这里映射到页面锚点
  const VIEW_ANCHORS = {
    loans: "#sec-loans",
    reservations: "#sec-reservations",
    fines: "#sec-fines",
    bookshelf: "#sec-bookshelf",
    reviews: "#sec-reviews",
    requests: "#sec-requests",
    profile: "#sec-profile",
  };

  function notifyIcon(kind) {
    return (
      {
        reservation_ready: "📗",
        reservation_expired: "⌛",
        due_soon: "⏰",
        overdue: "⚠",
        fine: "¥",
        profile: "🪪",
        book_request: "🛒",
        system: "📣",
      }[kind] || "🔔"
    );
  }

  function renderBell(unread) {
    state.notifyUnread = Number(unread || 0);
    const badge = $("#bellBadge");
    if (!badge) return;
    badge.hidden = state.notifyUnread <= 0;
    badge.textContent = state.notifyUnread > 99 ? "99+" : String(state.notifyUnread);
  }

  function renderNotifications(payload) {
    const box = $("#notifyList");
    const items = payload.items || [];
    if (!items.length) {
      box.innerHTML = `<div class="empty-state">
        <div class="empty-ico">🔔</div><p>暂无消息</p>
        <small>预约到书、即将到期、逾期提醒与审核结果都会汇总在这里</small>
      </div>`;
    } else {
      box.innerHTML = items
        .map((n) => {
          const unread = !n.read;
          return `<article class="notify-item${unread ? " is-unread" : ""}" data-notify="${n.id}">
            <div class="nt-ico" aria-hidden="true">${notifyIcon(n.kind)}</div>
            <div class="nt-main">
              <div class="nt-top">
                <span class="nt-kind">${esc(n.kind_label || n.kind)}</span>
                ${unread ? '<span class="nt-dot">未读</span>' : ""}
                <span class="nt-time">${esc(String(n.created_at || "").slice(0, 16))}</span>
              </div>
              <div class="nt-title">${esc(n.title)}</div>
              ${n.body ? `<div class="nt-body">${esc(n.body)}</div>` : ""}
            </div>
            <div class="nt-actions">
              ${
                n.link_view && VIEW_ANCHORS[n.link_view]
                  ? `<button class="btn btn-outline btn-mini" data-notify-go="${esc(n.link_view)}">查看</button>`
                  : ""
              }
              ${unread ? `<button class="btn btn-outline btn-mini" data-mark-read="${n.id}">标记已读</button>` : ""}
            </div>
          </article>`;
        })
        .join("");
    }
    $("#notifySummary").textContent = payload.unread
      ? `未读 ${payload.unread} 条 · 共 ${payload.total} 条`
      : `共 ${payload.total} 条`;
    renderBell(payload.unread);
    renderGenericPager("#notifyPager", payload);
  }

  async function loadNotifications() {
    if (state.gateOpen) return;
    const params = new URLSearchParams({
      kind: state.notifyKind,
      page: String(state.notifyPage),
      page_size: "10",
    });
    renderNotifications(await api.get("/api/reader/notifications?" + params.toString()));
  }

  /* ---------------- 我的书架（收藏） ---------------- */

  function renderShelf(payload) {
    const grid = $("#shelfGrid");
    const items = payload.items || [];
    if (!items.length) {
      grid.innerHTML = `<div class="empty-state">
        <div class="empty-ico">☆</div><p>书架还是空的</p>
        <small>收藏想读的书，之后在这里一眼就能找到</small>
      </div>`;
    } else {
      grid.innerHTML = items
        .map((f) => {
          const stateCls =
            Number(f.total_copies) <= 0 ? "badge-muted" : Number(f.available_copies) > 0 ? "badge-ok" : "badge-warn";
          return `<article class="shelf-card" data-fav="${f.id}">
            <div class="sc-head">
              <div class="sc-title">《${esc(f.book_title)}》</div>
              <span class="badge ${stateCls}">${esc(f.state_label)}</span>
            </div>
            <div class="sc-sub">${esc(f.book_author || "佚名")} · ${esc(f.book_category || "未分类")}</div>
            <div class="sc-sub">ISBN ${esc(f.book_isbn || "—")}${f.book_location ? " · " + esc(f.book_location) : ""}</div>
            ${f.rating_count ? `<div class="sc-rating">${starsHtml(f.rating_avg, f.rating_count, "sm")}</div>` : ""}
            ${f.note ? `<div class="sc-note">备注：${esc(f.note)}</div>` : ""}
            <div class="sc-foot">
              <span class="sc-time">${fmtDate(f.created_at)} 收藏</span>
              <div class="row-actions">
                <button class="btn btn-outline btn-mini" data-fav-note="${f.id}">备注</button>
                <button class="btn btn-outline btn-mini" data-fav-del="${f.id}">移除</button>
              </div>
            </div>
          </article>`;
        })
        .join("");
    }
    $("#shelfSummary").textContent = `共 ${payload.total} 本`;
    renderGenericPager("#shelfPager", payload);
  }

  function renderShelfCandidates(payload) {
    const box = $("#shelfCandidates");
    const items = payload.items || [];
    if (!items.length) {
      box.innerHTML = '<div class="shelf-hint">没有找到匹配的图书，换个关键词试试。</div>';
      return;
    }
    box.innerHTML = items
      .map((b) => {
        const total = Number(b.total_copies) || 0;
        const avail = Number(b.available_copies) || 0;
        const stateHtml =
          total <= 0
            ? '<span class="badge badge-muted">已下架</span>'
            : avail > 0
              ? `<span class="badge badge-ok">可借 ${avail} 册</span>`
              : '<span class="badge badge-warn">已借完</span>';
        return `<div class="shelf-cand">
          <div class="scc-main">
            <div class="scc-title">《${esc(b.title)}》</div>
            <div class="scc-sub">${esc(b.author || "佚名")} · ${esc(b.category || "未分类")}</div>
          </div>
          <div class="scc-state">${stateHtml}</div>
          <button class="btn btn-outline btn-mini" data-fav-add="${b.id}"${total <= 0 ? " disabled" : ""}>收藏</button>
        </div>`;
      })
      .join("");
  }

  async function loadShelf() {
    if (state.gateOpen) return;
    const params = new URLSearchParams({ page: String(state.shelfPage), page_size: "12" });
    if (state.shelfKeyword) params.set("keyword", state.shelfKeyword);
    renderShelf(await api.get("/api/reader/favorites?" + params.toString()));
  }

  /* ---------------- 评价 ---------------- */

  function renderReviewable(payload) {
    const box = $("#reviewableList");
    const items = payload.items || [];
    state.reviewPendingTotal = items.length;
    if (!items.length) {
      box.innerHTML = `<div class="empty-state">
        <div class="empty-ico">★</div><p>暂无可评价的图书</p>
        <small>借阅并归还之后，就可以在这里给书写个评价</small>
      </div>`;
    } else {
      box.innerHTML = items
        .map(
          (b) => `<div class="reviewable-item">
            <div class="rvi-main">
              <div class="rvi-title">《${esc(b.book_title)}》</div>
              <div class="rvi-sub">${esc(b.book_author || "佚名")} · 于 ${fmtDate(b.last_returned_at)} 归还</div>
            </div>
            <button class="btn btn-outline btn-mini" data-review="${b.book_id}" data-review-title="${esc(b.book_title)}">写评价</button>
          </div>`
        )
        .join("");
    }
    $("#reviewableSummary").textContent = items.length ? `${items.length} 本待评价` : "已全部评价";
  }

  function renderMyReviews(payload) {
    const box = $("#myReviewList");
    const items = payload.items || [];
    if (!items.length) {
      box.innerHTML = `<div class="empty-state">
        <div class="empty-ico">✎</div><p>还没有写过评价</p>
        <small>你的评价会出现在门户的图书详情里（只显示姓氏）</small>
      </div>`;
    } else {
      box.innerHTML = items
        .map(
          (r) => `<article class="my-review" data-review-id="${r.id}">
            <div class="mr-head">
              <div class="mr-title">《${esc(r.book_title)}》</div>
              <span class="badge ${r.status === "published" ? "badge-ok" : "badge-muted"}">${esc(r.status_label)}</span>
            </div>
            <div class="mr-stars">${starsHtml(r.rating, 1, "sm")}</div>
            ${
              r.content
                ? `<div class="mr-text">${esc(r.content)}</div>`
                : '<div class="mr-text mr-empty">（未填写短评）</div>'
            }
            <div class="mr-foot">
              <span>${esc(String(r.updated_at || r.created_at || "").slice(0, 16))}</span>
              <div class="row-actions">
                <button class="btn btn-outline btn-mini" data-review="${r.book_id}" data-review-title="${esc(r.book_title)}">修改</button>
                <button class="btn btn-outline btn-mini" data-review-del="${r.id}">删除</button>
              </div>
            </div>
          </article>`
        )
        .join("");
    }
    $("#myReviewSummary").textContent = `共 ${payload.total} 条`;
  }

  async function loadReviewable() {
    if (state.gateOpen) return;
    renderReviewable(await api.get("/api/reader/reviews/pending"));
  }

  async function loadMyReviews() {
    if (state.gateOpen) return;
    renderMyReviews(await api.get("/api/reader/reviews?page=1&page_size=50"));
  }

  function openReviewDialog(bookId, bookTitle, existing) {
    const initial = existing ? Number(existing.rating) : 0;
    openDialog({
      title: `评价《${bookTitle}》`,
      body: `<div class="field full" id="rvStarField">
          <label>评分<span class="req">*</span></label>
          ${starPickerHtml("rvStars", initial)}
        </div>
        <div class="field full">
          <label for="rvText">短评（选填，最多 500 字）</label>
          <textarea id="rvText" rows="4" maxlength="500" placeholder="说说这本书值得读的地方…">${esc(
            existing ? existing.content || "" : ""
          )}</textarea>
        </div>
        <p class="dlg-hint">门户上只会显示你的姓氏（如「张*」），不会公开完整姓名。</p>`,
      footer: `<button class="btn btn-outline" type="button" data-cancel>取消</button>
        <button class="btn btn-primary" type="button" data-submit>${existing ? "保存修改" : "发布评价"}</button>`,
      onMount(d) {
        let rating = initial;
        bindStarPicker(d.root, (v) => {
          rating = v;
        });
        d.root.querySelector("[data-cancel]").addEventListener("click", () => d.close());
        const submit = d.root.querySelector("[data-submit]");
        submit.addEventListener("click", async () => {
          if (!rating) {
            toast("请先选择星级", "点一下星星就能打分", "warn");
            return;
          }
          submit.disabled = true;
          submit.textContent = "提交中…";
          try {
            await api.post("/api/reader/reviews", {
              book_id: bookId,
              rating: rating,
              content: d.root.querySelector("#rvText").value.trim(),
            });
            toast(existing ? "评价已更新" : "评价已发布", "感谢你的分享，门户详情页会展示这条评价", "ok");
            d.close();
            await Promise.all([loadMyReviews(), loadReviewable(), loadNotifications()]);
          } catch (err) {
            showError(err);
            submit.disabled = false;
            submit.textContent = existing ? "保存修改" : "发布评价";
          }
        });
      },
    });
  }

  /* ---------------- 荐购登记 ---------------- */

  function renderBookRequests(payload) {
    const box = $("#bookRequestList");
    const items = payload.items || [];
    if (!items.length) {
      box.innerHTML = '<div class="req-empty">还没有荐购记录。想看的书馆里没有，就登记一下，馆员会看到。</div>';
    } else {
      box.innerHTML = items
        .map((r) => {
          const cls = r.pending ? "badge-info" : r.status === "rejected" ? "badge-muted" : "badge-ok";
          const meta = [r.author, r.publisher, r.isbn].filter(Boolean).join(" · ");
          return `<article class="request-item">
            <div class="ri-head">
              <div class="ri-title">《${esc(r.title)}》</div>
              <span class="badge ${cls}">${esc(r.status_label)}</span>
            </div>
            <div class="ri-sub">${esc(meta || "未填写作者 / 出版社 / ISBN")}</div>
            ${r.reason ? `<div class="ri-text">理由：${esc(r.reason)}</div>` : ""}
            ${r.reply ? `<div class="ri-reply">馆员答复：${esc(r.reply)}</div>` : ""}
            <div class="ri-foot">登记于 ${esc(String(r.created_at || "").slice(0, 16))}${
              r.reviewed_at ? ` · 处理于 ${esc(String(r.reviewed_at).slice(0, 16))}` : ""
            }</div>
          </article>`;
        })
        .join("");
    }
    $("#bookRequestSummary").textContent = payload.pending_total
      ? `${payload.pending_total} 条待处理 · 共 ${payload.total} 条`
      : `共 ${payload.total} 条`;
  }

  async function loadBookRequests() {
    if (state.gateOpen) return;
    renderBookRequests(await api.get("/api/reader/book-requests?page=1&page_size=20"));
  }

  /* ---------------- 资料变更申请 ---------------- */

  function currentPrValue() {
    const el = $("#prValue");
    return el ? String(el.value || "") : "";
  }

  /** 变更项从后端返回的 fields 生成，读者类型再给出可选项，避免前端写死两套口径。 */
  function syncProfileValueField(preserve) {
    const wrap = $("#fieldPrValue");
    const fieldSelect = $("#prField");
    if (!wrap || !fieldSelect) return;
    const field = fieldSelect.value;
    const meta = (state.profileFields || []).find((f) => f.field === field) || {};
    const current = String(meta.value || "");
    const keep = preserve ? currentPrValue() : "";
    const label = `新的${meta.label || "值"}`;
    if (field === "member_type") {
      const options = (state.memberTypes || [])
        .map((t) => `<option value="${esc(t)}"${t === (keep || current) ? " selected" : ""}>${esc(t)}</option>`)
        .join("");
      wrap.innerHTML = `<label for="prValue">${esc(label)}<span class="req">*</span></label>
        <select id="prValue">${options}</select>
        <span class="pr-current">当前为「${esc(current)}」</span>
        <span class="field-error" hidden></span>`;
    } else {
      wrap.innerHTML = `<label for="prValue">${esc(label)}<span class="req">*</span></label>
        <input id="prValue" type="text" value="${esc(keep)}" placeholder="请输入要改成的内容" />
        <span class="pr-current">当前为「${esc(current)}」</span>
        <span class="field-error" hidden></span>`;
    }
  }

  function renderProfileRequestForm() {
    const select = $("#prField");
    if (!select) return;
    const fields = state.profileFields || [];
    if (!fields.length) return;
    const keep = select.value;
    select.innerHTML = fields
      .map((f) => `<option value="${esc(f.field)}">${esc(f.label)}</option>`)
      .join("");
    if (keep && fields.some((f) => f.field === keep)) select.value = keep;
    syncProfileValueField(true);
  }

  function renderProfileRequests(payload) {
    const box = $("#profileRequestList");
    const items = payload.items || [];
    if (!items.length) {
      box.innerHTML = '<div class="req-empty">还没有提交过资料变更申请。</div>';
    } else {
      box.innerHTML = items
        .map((r) => {
          const cls = r.pending ? "badge-info" : r.status === "approved" ? "badge-ok" : "badge-muted";
          return `<article class="request-item">
            <div class="ri-head">
              <div class="ri-title">${esc(r.field_label || r.field)}：${esc(r.old_value || "—")} → ${esc(r.new_value)}</div>
              <span class="badge ${cls}">${esc(r.status_label)}</span>
            </div>
            ${r.reason ? `<div class="ri-text">说明：${esc(r.reason)}</div>` : ""}
            ${r.reply ? `<div class="ri-reply">馆员答复：${esc(r.reply)}</div>` : ""}
            <div class="ri-foot">提交于 ${esc(String(r.created_at || "").slice(0, 16))}${
              r.reviewed_at ? ` · 处理于 ${esc(String(r.reviewed_at).slice(0, 16))}` : ""
            }</div>
          </article>`;
        })
        .join("");
    }
  }

  async function loadProfileRequests() {
    if (state.gateOpen) return;
    const payload = await api.get("/api/reader/profile-requests?page=1&page_size=20");
    state.profileFields = payload.fields || [];
    state.memberTypes = payload.member_types || [];
    renderProfileRequestForm();
    renderProfileRequests(payload);
  }

  /* ---------------- 电子借书证 ---------------- */

  function renderECard() {
    const member = state.member;
    if (!member) return;
    $("#ecName").textContent = member.name || "—";
    $("#ecNo").textContent = member.card_no || "—";
    $("#ecType").textContent = member.member_type || "—";
    $("#ecStatus").textContent = member.card_lost ? "已挂失" : member.status === "active" ? "正常" : "已冻结";

    const code = String(member.card_no || "");
    const qrBox = $("#ecQr");
    const bcBox = $("#ecBarcode");
    // 二维码只编码证号本身：不含姓名 / 手机号，泄漏面最小
    if (qrBox) {
      try {
        qrBox.innerHTML = code && window.QRCode ? window.QRCode.svg(code, { scale: 5, level: "M", label: `借书证 ${code}` }) : "";
      } catch (err) {
        qrBox.innerHTML = '<div class="ec-fallback">二维码生成失败，请使用下方条码</div>';
      }
    }
    if (bcBox) {
      try {
        bcBox.innerHTML =
          code && window.Barcode ? window.Barcode.svg(code, { height: 56, moduleWidth: 1.7, showText: true }) : "";
      } catch (err) {
        bcBox.innerHTML = '<div class="ec-fallback">条码生成失败，请使用证号核对</div>';
      }
    }
    const foot = $("#ecFoot");
    if (foot) {
      foot.textContent = member.card_lost
        ? "此证已挂失，无法用于借书、续借与预约"
        : "馆员扫条码办借还，手机扫二维码可核对身份";
    }
  }

  /* ---------------- 导出 CSV ---------------- */

  function loanStateText(loan) {
    if (loan.status === "returned") return "已归还";
    if (loan.status === "overdue" || loan.overdue_days > 0) return `逾期 ${loan.overdue_days || 0} 天`;
    return "在借";
  }

  async function exportLoans() {
    const rows = await collectAll("/api/reader/loans", { status: state.status });
    downloadCsv(
      `我的借阅-${state.status}-${fileStamp()}.csv`,
      ["书名", "作者", "ISBN", "馆藏条码", "借出日期", "应还日期", "归还日期", "状态", "逾期天数", "续借次数", "滞纳金"],
      rows.map((l) => [
        l.book_title,
        l.book_author || "",
        l.book_isbn || "",
        l.book_barcode || "",
        fmtDate(l.borrowed_at),
        fmtDate(l.due_at),
        l.returned_at ? fmtDate(l.returned_at) : "",
        loanStateText(l),
        l.overdue_days || 0,
        l.renew_count || 0,
        Number(l.fine || 0).toFixed(2),
      ])
    );
  }

  async function exportReservations() {
    const rows = await collectAll("/api/reader/reservations", { status: "all" });
    downloadCsv(
      `我的预约-${fileStamp()}.csv`,
      ["书名", "作者", "ISBN", "预约时间", "状态", "排队位次", "到书时间", "取书截止", "备注"],
      rows.map((r) => [
        r.book_title,
        r.book_author || "",
        r.book_isbn || "",
        fmtDate(r.created_at),
        r.status_label || r.status,
        r.queue_position || "",
        r.ready_at ? fmtDate(r.ready_at) : "",
        r.expires_at ? String(r.expires_at).slice(0, 16) : "",
        r.note || "",
      ])
    );
  }

  async function exportFavorites() {
    const params = state.shelfKeyword ? { keyword: state.shelfKeyword } : {};
    const rows = await collectAll("/api/reader/favorites", params);
    downloadCsv(
      `我的书架-${fileStamp()}.csv`,
      ["书名", "作者", "ISBN", "馆藏条码", "分类", "馆藏位置", "馆藏状态", "评分", "评价数", "备注", "收藏时间"],
      rows.map((f) => [
        f.book_title,
        f.book_author || "",
        f.book_isbn || "",
        f.book_barcode || "",
        f.book_category || "",
        f.book_location || "",
        f.state_label || "",
        Number(f.rating_avg || 0).toFixed(1),
        f.rating_count || 0,
        f.note || "",
        String(f.created_at || "").slice(0, 16),
      ])
    );
  }

  function exportFines() {
    const items = (state.fines && state.fines.items) || [];
    downloadCsv(
      `我的滞纳金-${fileStamp()}.csv`,
      ["书名", "状态", "应还日期", "逾期天数", "应缴", "已缴", "待缴", "是否封顶"],
      items.map((f) => [
        f.book_title,
        f.state_label || "",
        fmtDate(f.due_at),
        f.overdue_days || 0,
        Number(f.fine || 0).toFixed(2),
        Number(f.settled || 0).toFixed(2),
        Number(f.outstanding || 0).toFixed(2),
        f.capped ? "是" : "否",
      ])
    );
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
    renderECard();
    renderBell(payload.notifications && payload.notifications.unread);
    // 初始密码未改：先让读者改密码，其余接口此刻会被后端拦下（403）
    if (payload.member && payload.member.must_change_password) showPasswordGate();
    else hidePasswordGate();
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

  /** 改密之外的全部数据（顺序加载更省事：任一个 403 都不会把其它请求带崩）。 */
  async function loadEverything() {
    await loadMe();
    if (state.gateOpen) return;
    await Promise.all([
      loadLoans(),
      loadReservations(),
      loadFines(),
      loadNotifications(),
      loadShelf(),
      loadReviewable(),
      loadMyReviews(),
      loadBookRequests(),
      loadProfileRequests(),
    ]);
  }

  async function refresh() {
    await loadMe();
    if (state.gateOpen) return;
    await Promise.all([loadLoans(), loadReservations(), loadFines()]);
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
        const subscribedBox = $("#pEmailSubscribed");
        const member = await api.put("/api/reader/profile", {
          phone: $("#pPhone").value.trim(),
          email: $("#pEmail").value.trim(),
          email_subscribed: subscribedBox ? subscribedBox.checked : true,
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

    /* ---- 强制改密遮罩 ---- */
    $("#pwGateForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      clearFieldErrors();
      const oldPassword = $("#gateOld").value;
      const newPassword = $("#gateNew").value;
      const confirmPassword = $("#gateNew2").value;
      if (!oldPassword) return markFieldError("fieldGateOld", "请输入当前密码");
      if (newPassword.length < 6) return markFieldError("fieldGateNew", "新密码至少 6 位");
      if (newPassword === oldPassword) return markFieldError("fieldGateNew", "新密码不能与当前密码相同");
      if (newPassword !== confirmPassword) return markFieldError("fieldGateNew2", "两次输入的新密码不一致");

      const btn = $("#gateBtn");
      btn.disabled = true;
      btn.textContent = "提交中…";
      try {
        await api.post("/api/reader/password", { old_password: oldPassword, new_password: newPassword });
        toast("密码已设置", "正在重新加载你的借阅信息…", "ok");
        hidePasswordGate();
        csrfToken = "";
        // 会话已在后端随改密失效，重新登录一次最稳妥
        setTimeout(() => location.replace("/reader"), 900);
      } catch (err) {
        if (err.field === "old_password") markFieldError("fieldGateOld", err.message);
        else if (err.field === "new_password") markFieldError("fieldGateNew", err.message);
        else showError(err);
        btn.disabled = false;
        btn.textContent = "设置新密码并继续";
      }
    });

    $("#gateLogout").addEventListener("click", async () => {
      try {
        await api.post("/api/reader/logout");
      } catch (err) {
        /* 登录态已失效也照样回首页 */
      }
      location.replace("/");
    });

    /* ---- 消息中心 ---- */
    $("#bellBtn").addEventListener("click", () => {
      const section = $("#sec-notifications");
      if (!section) return;
      section.scrollIntoView({ behavior: "smooth", block: "start" });
      loadNotifications().catch(showError);
    });

    $("#notifyTabs").addEventListener("click", (e) => {
      const btn = e.target.closest(".tab");
      if (!btn) return;
      $$(".tab", $("#notifyTabs")).forEach((t) => t.classList.toggle("is-active", t === btn));
      state.notifyKind = btn.dataset.kind;
      state.notifyPage = 1;
      loadNotifications().catch(showError);
    });

    $("#notifyPager").addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-page]");
      if (!btn || btn.disabled) return;
      state.notifyPage = Number(btn.dataset.page);
      loadNotifications().catch(showError);
    });

    $("#readAllBtn").addEventListener("click", async () => {
      const btn = $("#readAllBtn");
      btn.disabled = true;
      btn.textContent = "处理中…";
      try {
        const res = await api.post("/api/reader/notifications/read-all");
        toast("已全部标记已读", `共更新 ${res.marked || 0} 条`, "ok");
        await loadNotifications();
      } catch (err) {
        showError(err);
      } finally {
        btn.disabled = false;
        btn.textContent = "全部标记已读";
      }
    });

    $("#notifyList").addEventListener("click", async (e) => {
      const goBtn = e.target.closest("[data-notify-go]");
      if (goBtn) {
        const anchor = VIEW_ANCHORS[goBtn.dataset.notifyGo];
        if (anchor) $(anchor).scrollIntoView({ behavior: "smooth", block: "start" });
        return;
      }
      const readBtn = e.target.closest("[data-mark-read]");
      if (readBtn) {
        readBtn.disabled = true;
        try {
          await api.post(`/api/reader/notifications/${readBtn.dataset.markRead}/read`);
          await loadNotifications();
        } catch (err) {
          showError(err);
        }
        return;
      }
      // 点卡片空白处：未读则顺手标记已读
      const card = e.target.closest(".notify-item");
      if (card && card.classList.contains("is-unread")) {
        try {
          await api.post(`/api/reader/notifications/${card.dataset.notify}/read`);
          await loadNotifications();
        } catch (err) {
          /* 顺手标记失败不影响阅读 */
        }
      }
    });

    /* ---- 我的书架 ---- */
    $("#shelfAddForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const box = $("#shelfCandidates");
      const btn = $("#shelfSearchBtn");
      box.innerHTML = '<div class="shelf-hint">搜索中…</div>';
      btn.disabled = true;
      try {
        const keyword = $("#shelfKeyword").value.trim();
        renderShelfCandidates(
          await api.get("/api/reader/books/search?page_size=8&keyword=" + encodeURIComponent(keyword))
        );
      } catch (err) {
        showError(err);
        box.innerHTML = '<div class="shelf-hint">搜索失败，请稍后重试。</div>';
      } finally {
        btn.disabled = false;
      }
    });

    $("#shelfCandidates").addEventListener("click", async (e) => {
      const btn = e.target.closest("button[data-fav-add]");
      if (!btn) return;
      btn.disabled = true;
      btn.textContent = "收藏中…";
      try {
        const fav = await api.post("/api/reader/favorites", { book_id: Number(btn.dataset.favAdd) });
        toast("已加入书架", `《${fav.book_title}》现在可以在「我的书架」里找到`, "ok");
        btn.textContent = "已收藏";
        state.shelfPage = 1;
        await loadShelf();
      } catch (err) {
        showError(err);
        btn.disabled = false;
        btn.textContent = "收藏";
      }
    });

    $("#shelfGrid").addEventListener("click", async (e) => {
      const delBtn = e.target.closest("button[data-fav-del]");
      if (delBtn) {
        if (!confirm("确定从书架里移除这本书吗？")) return;
        delBtn.disabled = true;
        try {
          await api.del(`/api/reader/favorites/${delBtn.dataset.favDel}`);
          toast("已移除", "这本书已从你的书架移除", "ok");
          await loadShelf();
        } catch (err) {
          showError(err);
          delBtn.disabled = false;
        }
        return;
      }
      const noteBtn = e.target.closest("button[data-fav-note]");
      if (noteBtn) {
        const card = noteBtn.closest(".shelf-card");
        const current = (card.querySelector(".sc-note") || {}).textContent || "";
        openDialog({
          title: "编辑书架备注",
          body: `<div class="field full">
              <label for="favNote">备注（选填，最多 200 字）</label>
              <textarea id="favNote" rows="3" maxlength="200" placeholder="例如：老师推荐，期末前读完">${esc(
                current.replace(/^备注：/, "")
              )}</textarea>
            </div>`,
          footer: `<button class="btn btn-outline" type="button" data-cancel>取消</button>
            <button class="btn btn-primary" type="button" data-save>保存</button>`,
          onMount(d) {
            d.root.querySelector("[data-cancel]").addEventListener("click", () => d.close());
            const save = d.root.querySelector("[data-save]");
            save.addEventListener("click", async () => {
              save.disabled = true;
              save.textContent = "保存中…";
              try {
                await api.put(`/api/reader/favorites/${noteBtn.dataset.favNote}`, {
                  note: d.root.querySelector("#favNote").value.trim(),
                });
                toast("已保存", "备注已更新", "ok");
                d.close();
                await loadShelf();
              } catch (err) {
                showError(err);
                save.disabled = false;
                save.textContent = "保存";
              }
            });
          },
        });
      }
    });

    $("#shelfPager").addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-page]");
      if (!btn || btn.disabled) return;
      state.shelfPage = Number(btn.dataset.page);
      loadShelf().catch(showError);
    });

    /* ---- 评价 ---- */
    $("#reviewableList").addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-review]");
      if (!btn) return;
      openReviewDialog(Number(btn.dataset.review), btn.dataset.reviewTitle || "", null);
    });

    $("#myReviewList").addEventListener("click", async (e) => {
      const delBtn = e.target.closest("button[data-review-del]");
      if (delBtn) {
        if (!confirm("确定删除这条评价吗？删除后门户上不再展示。")) return;
        delBtn.disabled = true;
        try {
          await api.del(`/api/reader/reviews/${delBtn.dataset.reviewDel}`);
          toast("已删除", "这条评价已移除", "ok");
          await Promise.all([loadMyReviews(), loadReviewable(), loadNotifications()]);
        } catch (err) {
          showError(err);
          delBtn.disabled = false;
        }
        return;
      }
      const editBtn = e.target.closest("button[data-review]");
      if (editBtn) {
        const card = editBtn.closest(".my-review");
        const text = (card.querySelector(".mr-text") || {}).textContent || "";
        const onStars = card.querySelectorAll(".mr-stars .stars i.on").length;
        openReviewDialog(Number(editBtn.dataset.review), editBtn.dataset.reviewTitle || "", {
          rating: onStars,
          content: /^（未填写短评）$/.test(text.trim()) ? "" : text.trim(),
        });
      }
    });

    /* ---- 荐购登记 ---- */
    $("#bookRequestForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      clearFieldErrors();
      const title = $("#brTitle").value.trim();
      if (!title) return markFieldError("fieldBrTitle", "请填写书名");
      const btn = $("#bookRequestBtn");
      btn.disabled = true;
      btn.textContent = "提交中…";
      try {
        const result = await api.post("/api/reader/book-requests", {
          title: title,
          author: $("#brAuthor").value.trim(),
          publisher: $("#brPublisher").value.trim(),
          isbn: $("#brIsbn").value.trim(),
          reason: $("#brReason").value.trim(),
        });
        toast("荐购已登记", `《${result.title}》已提交，馆员处理后会通知你`, "ok");
        $("#bookRequestForm").reset();
        await loadBookRequests();
      } catch (err) {
        if (err.field === "title") markFieldError("fieldBrTitle", err.message);
        else if (err.field === "isbn") markFieldError("fieldBrIsbn", err.message);
        else showError(err);
      } finally {
        btn.disabled = false;
        btn.textContent = "提交荐购";
      }
    });

    /* ---- 资料变更申请 ---- */
    $("#prField").addEventListener("change", () => syncProfileValueField(false));

    $("#profileRequestForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      clearFieldErrors();
      const field = $("#prField").value;
      const newValue = currentPrValue().trim();
      if (!newValue) return markFieldError("fieldPrValue", "请填写新的值");
      const btn = $("#profileRequestBtn");
      btn.disabled = true;
      btn.textContent = "提交中…";
      try {
        const result = await api.post("/api/reader/profile-requests", {
          field: field,
          new_value: newValue,
          reason: $("#prReason").value.trim(),
        });
        toast("申请已提交", `${result.field_label || "资料"}变更申请已提交，馆员审核后会通知你`, "ok");
        $("#prReason").value = "";
        await loadProfileRequests();
      } catch (err) {
        if (err.field === "new_value") markFieldError("fieldPrValue", err.message);
        else if (err.field === "field") markFieldError("fieldPrField", err.message);
        else showError(err);
      } finally {
        btn.disabled = false;
        btn.textContent = "提交申请";
      }
    });

    /* ---- 预约改约 ---- */
    $("#reservationBody").addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-rebook-res]");
      if (!btn) return;
      openRebookDialog(Number(btn.dataset.rebookRes));
    });

    /* ---- 电子借书证 ---- */
    $("#printCardBtn").addEventListener("click", () => window.print());

    /* ---- 导出 CSV ---- */
    $("#exportLoansBtn").addEventListener("click", () => withBusy("#exportLoansBtn", "导出中…", exportLoans));
    $("#exportResBtn").addEventListener("click", () => withBusy("#exportResBtn", "导出中…", exportReservations));
    $("#exportFavBtn").addEventListener("click", () => withBusy("#exportFavBtn", "导出中…", exportFavorites));
    $("#exportFineBtn").addEventListener("click", exportFines);
  }

  /** 导出这类可能稍慢的动作：按钮临时置忙，失败统一弹错。 */
  async function withBusy(selector, busyText, action) {
    const btn = $(selector);
    const original = btn.textContent;
    btn.disabled = true;
    btn.textContent = busyText;
    try {
      await action();
    } catch (err) {
      showError(err);
    } finally {
      btn.disabled = false;
      btn.textContent = original;
    }
  }

  /** 改约弹窗：搜到目标书后一键换单（关旧单 + 开新单在后端同一事务里完成）。 */
  function openRebookDialog(resId) {
    openDialog({
      title: "改约到其它图书",
      body: `<div class="rebook-search">
          <input id="rebookKeyword" type="text" placeholder="输入书名 / 作者 / ISBN / 条码" autocomplete="off" />
          <button class="btn btn-primary btn-mini" type="button" data-search>搜索</button>
        </div>
        <div class="rebook-results" id="rebookResults">
          <div class="reserve-hint">搜索「已借完」的图书，点击「改为预约这本」即可替换。</div>
        </div>`,
      onMount(d) {
        const box = d.root.querySelector("#rebookResults");
        const input = d.root.querySelector("#rebookKeyword");
        const searchBtn = d.root.querySelector("[data-search]");

        async function doSearch() {
          box.innerHTML = '<div class="reserve-hint">搜索中…</div>';
          searchBtn.disabled = true;
          try {
            const payload = await api.get(
              "/api/reader/books/search?page_size=8&keyword=" + encodeURIComponent(input.value.trim())
            );
            const items = payload.items || [];
            if (!items.length) {
              box.innerHTML = '<div class="reserve-hint">没有找到匹配的图书，换个关键词试试。</div>';
              return;
            }
            box.innerHTML = items
              .map((b) => {
                const total = Number(b.total_copies) || 0;
                const avail = Number(b.available_copies) || 0;
                const selectable = total > 0 && avail === 0;
                const stateHtml =
                  total <= 0
                    ? '<span class="badge badge-muted">已下架</span>'
                    : avail > 0
                      ? `<span class="badge badge-ok">可借 ${avail} 册</span>`
                      : '<span class="badge badge-warn">已借完</span>';
                return `<div class="reserve-item${selectable ? "" : " is-unavailable"}">
                  <div class="ri-main">
                    <div class="ri-title">《${esc(b.title)}》</div>
                    <div class="ri-sub">${esc(b.author || "佚名")} · ${esc(b.category || "未分类")}</div>
                  </div>
                  <div class="ri-state">${stateHtml}</div>
                  <button class="btn btn-outline btn-mini" type="button" data-pick="${b.id}"${
                    selectable ? "" : " disabled"
                  }>改为预约这本</button>
                </div>`;
              })
              .join("");
          } catch (err) {
            showError(err);
            box.innerHTML = '<div class="reserve-hint">搜索失败，请稍后重试。</div>';
          } finally {
            searchBtn.disabled = false;
          }
        }

        searchBtn.addEventListener("click", doSearch);
        input.addEventListener("keydown", (e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            doSearch();
          }
        });

        box.addEventListener("click", async (e) => {
          const btn = e.target.closest("button[data-pick]");
          if (!btn) return;
          btn.disabled = true;
          btn.textContent = "改约中…";
          try {
            const result = await api.post(`/api/reader/reservations/${resId}/rebook`, {
              book_id: Number(btn.dataset.pick),
            });
            const book = result.reservation || {};
            toast("改约成功", `已改为预约《${book.book_title || ""}》`, "ok");
            d.close();
            await Promise.all([loadReservations(), loadMe(), loadNotifications()]);
          } catch (err) {
            showError(err);
            btn.disabled = false;
            btn.textContent = "改为预约这本";
          }
        });
      },
    });
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
    // 页面上的字段较多（改密 / 遮罩 / 荐购 / 资料变更），统一按类名扫，避免漏掉某个新表单
    $$(".field.has-error").forEach((field) => {
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
    if (!field) return;
    field.classList.add("has-error");
    const tip = field.querySelector(".field-error");
    if (tip) {
      tip.hidden = false;
      tip.textContent = message;
    }
    const input = field.querySelector("input, select, textarea");
    if (input && typeof input.focus === "function") input.focus();
  }

  document.addEventListener("DOMContentLoaded", () => {
    bindEvents();
    loadEverything().catch((err) => {
      if (err && err.code === "unauthorized") return;
      showError(err);
    });
  });
})();
