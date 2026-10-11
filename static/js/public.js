/* 读者门户：馆藏检索 + 推荐 */
(function () {
  "use strict";

  const $ = (sel, root) => (root || document).querySelector(sel);

  const esc = (v) => {
    if (v === null || v === undefined) return "";
    return String(v)
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

  const money = (v) => "¥" + Number(v || 0).toFixed(2);

  const toast = (title, body, kind) => {
    const layer = $("#toastLayer");
    const el = document.createElement("div");
    el.className = "toast " + (kind || "ok");
    el.setAttribute("role", kind === "error" ? "alert" : "status");
    el.innerHTML =
      '<button class="t-close" type="button" aria-label="关闭提示">✕</button>' +
      `<div class="t-title">${esc(title)}</div>` +
      (body ? `<div class="t-body">${esc(body)}</div>` : "");
    layer.appendChild(el);

    const wait = kind === "error" ? 5200 : 2800;
    let timer = setTimeout(dismiss, wait);
    function dismiss() {
      clearTimeout(timer);
      el.classList.add("out");
      setTimeout(() => el.remove(), 220);
    }
    el.querySelector(".t-close").addEventListener("click", dismiss);
    el.addEventListener("mouseenter", () => clearTimeout(timer));
    el.addEventListener("mouseleave", () => { timer = setTimeout(dismiss, wait); });
  };

  const getJSON = async (path, params) => {
    const qs = params ? "?" + new URLSearchParams(params).toString() : "";
    let response;
    try {
      response = await fetch(path + qs, { headers: { Accept: "application/json" } });
    } catch (err) {
      const error = new Error("无法连接服务器，请稍后重试");
      error.network = true;
      throw error;
    }
    const text = await response.text();
    let payload = null;
    if (text) { try { payload = JSON.parse(text); } catch (err) { payload = null; } }
    if (!response.ok) {
      const message = (payload && payload.error && payload.error.message) || `请求失败（HTTP ${response.status}）`;
      throw new Error(message);
    }
    return payload;
  };

  const state = { keyword: "", category: "", availableOnly: false, page: 1, pageSize: 8 };
  let searchStarted = false;

  /* 已渲染过的书目：详情弹窗直接取用，避免二次请求 */
  const bookCache = new Map();
  let lastFocused = null;

  const cacheBooks = (list) => (list || []).forEach((b) => bookCache.set(String(b.id), b));

  /* ---------------- 概览 ---------------- */

  function renderStats(stats) {
    const items = [
      { v: stats.total_titles || 0, u: "种", k: "馆藏书目" },
      { v: stats.total_copies || 0, u: "册", k: "馆藏总量" },
      { v: stats.available_copies || 0, u: "册", k: "当前可借" },
      { v: stats.borrowable_titles || 0, u: "种", k: "可借书目" },
      { v: stats.active_reader_count || 0, u: "人", k: "在册读者" },
    ];
    $("#statBar").innerHTML = items
      .map((it) => `<div class="statbox"><div class="v">${esc(it.v)}<small>${esc(it.u)}</small></div><div class="k">${esc(it.k)}</div></div>`)
      .join("");
  }

  function renderChips(categories) {
    $("#quickChips").innerHTML = categories
      .slice(0, 9)
      .map((c) => `<button type="button" class="chip" data-cat="${esc(c.name)}">${esc(c.name)} · ${esc(c.book_count)} 种</button>`)
      .join("");
    $("#quickChips").addEventListener("click", (e) => {
      const chip = e.target.closest(".chip");
      if (!chip) return;
      state.category = chip.dataset.cat;
      state.keyword = "";
      $("#q").value = "";
      $("#categoryFilter").value = chip.dataset.cat;
      state.page = 1;
      runSearch();
    });
  }

  function renderCategoryOptions(categories) {
    const sel = $("#categoryFilter");
    sel.innerHTML =
      `<option value="">全部分类</option>` +
      categories.map((c) => `<option value="${esc(c.name)}">${esc(c.name)}</option>`).join("");
  }

  function bookCard(b) {
    const avail = Number(b.available_copies) || 0;
    const badge = b.available
      ? `<span class="badge badge-ok">${esc(b.status_label)}</span>`
      : `<span class="badge badge-danger">全部借出</span>`;
    const meta = [b.category, b.publisher || "—", b.publish_year ? String(b.publish_year) : ""].filter(Boolean);
    return `<article class="book-card" data-book="${esc(b.id)}" tabindex="0" role="button"
      aria-label="查看《${esc(b.title)}》的内容简介与馆藏条码">
      <div class="bc-top">
        <div>
          <div class="bc-title">${esc(b.title)}</div>
          <div class="bc-author">${esc(b.author || "佚名")}</div>
        </div>
        ${badge}
      </div>
      <p class="bc-desc${b.description ? "" : " is-empty"}">${esc(b.description || "本馆暂未录入这本书的内容简介")}</p>
      <div class="bc-meta">
        ${meta.map((m) => `<span>${esc(m)}</span>`).join("")}
        ${b.barcode ? `<span class="bc-code">条码 ${esc(b.barcode)}</span>` : ""}
      </div>
      <div class="bc-foot">
        <span class="shelf">馆藏位置 <b>${esc(b.location || "待分配")}</b></span>
        <span class="bc-foot-right">
          <span class="badge badge-muted">${avail}/${Number(b.total_copies) || 0} 册</span>
          <span class="bc-more">详情 ›</span>
        </span>
      </div>
    </article>`;
  }

  function renderCards(container, list, emptyText) {
    cacheBooks(list);
    container.innerHTML = list.length
      ? list.map(bookCard).join("")
      : `<div class="empty-state"><p>${esc(emptyText)}</p></div>`;
    bindCardClicks(container);
  }

  /** 整张书卡可点：打开详情弹窗（事件委托只绑一次） */
  function bindCardClicks(container) {
    if (container.dataset.cardBound === "1") return;
    container.dataset.cardBound = "1";
    const open = (event) => {
      const card = event.target.closest(".book-card");
      if (card && card.dataset.book) openBookModal(card.dataset.book);
    };
    container.addEventListener("click", open);
    container.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        const card = event.target.closest(".book-card");
        if (card) {
          event.preventDefault();
          openBookModal(card.dataset.book);
        }
      }
    });
  }

  /* ---------------- 图书详情弹窗（简介 + 馆藏条码） ---------------- */

  function detailHtml(b) {
    const avail = Number(b.available_copies) || 0;
    const total = Number(b.total_copies) || 0;
    const badge = b.available
      ? `<span class="badge ${avail === 1 ? "badge-warn" : "badge-ok"}">${esc(b.status_label)}</span>`
      : `<span class="badge badge-danger">全部借出</span>`;
    return `<div class="bk-detail">
      <div class="bk-detail-main">
        <h4>《${esc(b.title)}》</h4>
        <div class="bk-detail-sub">
          ${esc(b.author || "佚名")} · ${esc(b.publisher || "未标注出版社")}${b.publish_year ? ` · ${esc(b.publish_year)} 年` : ""}
        </div>
        <div class="bk-tags">
          <span class="badge badge-muted">${esc(b.category)}</span>
          ${badge}
          <span class="badge badge-muted">馆藏位置 ${esc(b.location || "待分配")}</span>
        </div>
        <p class="bk-desc-full${b.description ? "" : " is-empty"}">${esc(
          b.description || "本馆暂未录入这本书的内容简介，可到馆向馆员咨询。"
        )}</p>
        <div class="bk-meta">
          <div><span>馆藏条码</span><b class="mono">${esc(b.barcode || "—")}</b></div>
          <div><span>ISBN</span><b class="mono">${esc(b.isbn)}</b></div>
          <div><span>馆藏册数</span><b>可借 ${avail} / 共 ${total} 册</b></div>
        </div>
      </div>
      <div class="bk-code-box">
        ${b.barcode
          ? Barcode.svg(b.barcode, { height: 52 })
          : '<div class="bk-code-tip">该书尚未设置条码</div>'}
        <div class="bk-code-tip">到馆报此条码可快速找书</div>
      </div>
    </div>`;
  }

  function openBookModal(id) {
    const book = bookCache.get(String(id));
    if (!book) {
      toast("无法打开详情", "请重新检索后再试", "error");
      return;
    }
    lastFocused = document.activeElement;
    $("#bookModalTitle").textContent = "图书详情";
    $("#bookModalBody").innerHTML = detailHtml(book);
    $("#bookModal").hidden = false;
    $("#bookModalClose").focus();
  }

  function closeBookModal() {
    $("#bookModal").hidden = true;
    $("#bookModalBody").innerHTML = "";
    if (lastFocused && lastFocused.focus) lastFocused.focus();
  }

  /** 罚金说明：把封顶规则如实告诉读者，避免「越拖越多」的误解。 */
  function fineCapNote(rules) {
    const cap = Number(rules.fine_cap) || 0;
    const days = Number(rules.fine_max_days) || 0;
    if (cap > 0 && days > 0) return `按天累计，${days} 天或 ${money(cap)} 封顶`;
    if (cap > 0) return `按天累计，单笔封顶 ${money(cap)}`;
    if (days > 0) return `按天累计，最长计息 ${days} 天`;
    return "按逾期天数累计";
  }

  function renderRules(rules) {
    const cards = [
      { ico: "📖", v: rules.loan_days, u: "天", k: "单次借期", d: "自借出之日起计算" },
      { ico: "🔁", v: rules.max_renew, u: "次", k: "可续借次数", d: "每次续借顺延一个借期" },
      { ico: "💰", v: money(rules.fine_per_day), u: "", k: "逾期罚金", d: fineCapNote(rules) },
      { ico: "🎫", v: rules.max_borrow, u: "册", k: "默认可借上限", d: "教师与 VIP 读者更高" },
    ];
    $("#ruleGrid").innerHTML = cards
      .map(
        (c) => `<div class="rule">
          <div class="r-ico">${c.ico}</div>
          <div class="r-v">${esc(c.v)}${c.u ? `<small>${esc(c.u)}</small>` : ""}</div>
          <div class="r-k">${esc(c.k)}</div>
          <div class="r-d">${esc(c.d)}</div>
        </div>`
      )
      .join("");
  }

  async function loadOverview() {
    const data = await getJSON("/api/public/overview");
    renderStats(data.stats || {});
    renderCategoryOptions(data.categories || []);
    renderChips(data.categories || []);
    renderCards($("#recommendCards"), data.recommended || [], "暂无推荐书目");
    renderCards($("#newCards"), data.new_arrivals || [], "暂无新书");
    renderRules(data.rules || {});
  }

  /* ---------------- 检索 ---------------- */

  function renderSearchTable(payload) {
    const wrap = $("#searchResultWrap");
    const table = $("#searchTable");
    wrap.hidden = false;
    $("#searchEmpty").hidden = true;
    cacheBooks(payload.items);

    if (!payload.items.length) {
      wrap.hidden = true;
      $("#searchEmpty").hidden = false;
      $("#searchEmpty").innerHTML = `
        <div class="empty-ico">📭</div>
        <p>没有找到匹配「${esc(payload.keyword || state.keyword)}」的图书</p>
        <small>可尝试更换关键词、切换分类，或取消「仅看可借」筛选</small>`;
      $("#searchPager").hidden = true;
      return;
    }

    table.innerHTML = `
      <thead><tr>
        <th>书名 / 作者</th><th>馆藏条码</th><th>出版社</th><th>分类</th><th>馆藏位置</th><th>在架状态</th><th>馆藏册数</th><th>详情</th>
      </tr></thead>
      <tbody>${payload.items
        .map((b) => {
          const badge = b.available
            ? `<span class="badge ${Number(b.available_copies) === 1 ? "badge-warn" : "badge-ok"}">${esc(b.status_label)}</span>`
            : `<span class="badge badge-danger">全部借出</span>`;
          return `<tr>
            <td><strong>${esc(b.title)}</strong><div class="sub">${esc(b.author || "佚名")} · ${esc(b.isbn)}</div></td>
            <td>${b.barcode ? `<span class="barcode-tag">${esc(b.barcode)}</span>` : '<span class="sub">—</span>'}</td>
            <td>${esc(b.publisher || "—")}${b.publish_year ? `<div class="sub">${esc(b.publish_year)} 年</div>` : ""}</td>
            <td><span class="badge badge-muted">${esc(b.category)}</span></td>
            <td class="mono">${esc(b.location || "待分配")}</td>
            <td>${badge}</td>
            <td class="mono">${Number(b.available_copies) || 0} / ${Number(b.total_copies) || 0}</td>
            <td><button type="button" class="row-detail" data-book="${esc(b.id)}">简介</button></td>
          </tr>`;
        })
        .join("")}</tbody>`;

    const pager = $("#searchPager");
    pager.hidden = false;
    pager.innerHTML = `
      <span class="page-info">共 ${payload.total} 条结果 · 第 ${payload.page}/${payload.pages} 页</span>
      <button data-go="${payload.page - 1}" ${payload.page <= 1 ? "disabled" : ""}>上一页</button>
      <button data-go="${payload.page + 1}" ${payload.page >= payload.pages ? "disabled" : ""}>下一页</button>`;
    pager.querySelectorAll("[data-go]").forEach((btn) =>
      btn.addEventListener("click", () => {
        state.page = Number(btn.dataset.go);
        runSearch();
        document.getElementById("search").scrollIntoView({ behavior: "smooth", block: "start" });
      })
    );
  }

  async function runSearch() {
    const summary = $("#searchSummary");
    summary.textContent = "检索中…";
    try {
      const payload = await getJSON("/api/public/search", {
        keyword: state.keyword,
        category: state.category,
        available_only: state.availableOnly ? "1" : "",
        page: state.page,
        page_size: state.pageSize,
      });
      searchStarted = true;
      renderSearchTable(payload);
      const parts = [];
      if (payload.keyword) parts.push(`关键词「${payload.keyword}」`);
      if (state.category) parts.push(`分类「${state.category}」`);
      if (state.availableOnly) parts.push("仅可借");
      summary.textContent = `共 ${payload.total} 条${parts.length ? " · " + parts.join(" · ") : ""}`;
      $("#search").scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (err) {
      summary.textContent = "检索失败";
      toast("检索失败", err.message, "error");
    }
  }

  /* ---------------- 统一登录（右上角入口弹窗） ---------------- */

  let loginTrigger = null;

  function openLoginModal() {
    loginTrigger = document.activeElement;
    $("#loginModal").hidden = false;
    loginError("");
    $("#portalIdentifier").focus();
  }

  function closeLoginModal() {
    $("#loginModal").hidden = true;
    const fallback = $("#headerLoginBtn");
    const target =
      loginTrigger && loginTrigger !== document.body && loginTrigger.focus ? loginTrigger : fallback;
    if (target && target.focus) target.focus();
  }

  /** 登录弹窗的 Tab 焦点陷阱：焦点只在弹窗内循环 */
  function trapLoginTab(event) {
    const focusables = Array.from(
      $("#loginModal").querySelectorAll("button, input, a[href]")
    ).filter((el) => !el.disabled && el.offsetParent !== null);
    if (!focusables.length) return;
    const first = focusables[0];
    const last = focusables[focusables.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function loginError(message, field) {
    const box = $("#portalLoginError");
    if (!message) {
      box.hidden = true;
      box.textContent = "";
    } else {
      box.hidden = false;
      box.textContent = message;
    }
    ["#plFieldAccount", "#plFieldPassword"].forEach((sel) => $(sel).classList.remove("has-error"));
    if (field === "identifier" || field === "account" || field === "username") $("#plFieldAccount").classList.add("has-error");
    if (field === "password") $("#plFieldPassword").classList.add("has-error");
  }

  /** 登录后的落点：只认与当前身份匹配的站内相对地址，避免开放重定向 */
  function targetAfterLogin(payload) {
    const home = payload.redirect || "/";
    const raw = new URLSearchParams(location.search).get("next") || "";
    if (!raw.startsWith("/") || raw.startsWith("//")) return home;
    if (payload.role === "admin" && raw.startsWith("/admin")) return raw;
    if (payload.role === "reader" && raw.startsWith("/reader")) return raw;
    return home;
  }

  function bindLogin() {
    const form = $("#portalLoginForm");
    if (!form) return;
    const btn = $("#portalLoginBtn");
    const pw = $("#portalPassword");

    $("#portalPwToggle").addEventListener("click", () => {
      const showing = pw.type === "text";
      pw.type = showing ? "password" : "text";
      $("#portalPwToggle").textContent = showing ? "显示" : "隐藏";
    });

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      loginError("");

      const identifier = $("#portalIdentifier").value.trim();
      const password = pw.value;
      if (!identifier) return loginError("请输入账号", "identifier");
      if (!password) return loginError("请输入密码", "password");

      btn.disabled = true;
      const original = btn.textContent;
      btn.textContent = "登录中…";

      try {
        const response = await fetch("/api/auth/login", {
          method: "POST",
          headers: { "Content-Type": "application/json", Accept: "application/json" },
          body: JSON.stringify({ identifier, password }),
        });
        const text = await response.text();
        let payload = null;
        if (text) { try { payload = JSON.parse(text); } catch (err) { payload = null; } }

        if (!response.ok) {
          const errObj = (payload && payload.error) || {};
          loginError(errObj.message || `登录失败（HTTP ${response.status}）`, errObj.details && errObj.details.field);
          btn.disabled = false;
          btn.textContent = original;
          return;
        }

        const label = payload.role === "admin" ? "管理员" : "读者";
        btn.textContent = `${label}登录成功，正在进入…`;
        location.replace(targetAfterLogin(payload));
      } catch (err) {
        loginError("无法连接服务器，请确认服务是否在运行");
        btn.disabled = false;
        btn.textContent = original;
      }
    });
  }

  /* ---------------- 事件 ---------------- */

  function bind() {
    $("#searchForm").addEventListener("submit", (e) => {
      e.preventDefault();
      state.keyword = $("#q").value.trim();
      state.page = 1;
      runSearch();
    });
    $("#q").addEventListener("input", debounce((e) => {
      const value = e.target.value.trim();
      if (!value && !searchStarted) return;
      state.keyword = value;
      state.page = 1;
      runSearch();
    }, 420));

    $("#categoryFilter").addEventListener("change", (e) => {
      state.category = e.target.value;
      state.page = 1;
      runSearch();
    });

    $("#availableOnly").addEventListener("change", (e) => {
      state.availableOnly = e.target.checked;
      state.page = 1;
      if (searchStarted || state.keyword) runSearch();
    });

    // 检索结果里的「简介」按钮
    $("#searchTable").addEventListener("click", (e) => {
      const btn = e.target.closest("[data-book]");
      if (btn) openBookModal(btn.dataset.book);
    });

    // 详情弹窗：关闭按钮 / 遮罩 / Esc
    $("#bookModalClose").addEventListener("click", closeBookModal);
    $("#bookModal").addEventListener("click", (e) => {
      if (e.target === $("#bookModal")) closeBookModal();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape") return;
      if (!$("#bookModal").hidden) closeBookModal();
      else if (!$("#loginModal").hidden) closeLoginModal();
    });

    // 登录弹窗：右上角按钮 / 页面内「前往登录」「登录入口」均可打开
    $("#headerLoginBtn").addEventListener("click", openLoginModal);
    document.querySelectorAll("[data-open-login]").forEach((el) => {
      el.addEventListener("click", (e) => {
        e.preventDefault();
        openLoginModal();
      });
    });
    $("#loginModalClose").addEventListener("click", closeLoginModal);
    $("#loginModal").addEventListener("click", (e) => {
      if (e.target === $("#loginModal")) closeLoginModal();
    });
    $("#loginModal").addEventListener("keydown", (e) => {
      if (e.key === "Tab") trapLoginTab(e);
    });

    // 直链 #login（如旧链接 / 刷新后）也走弹窗，并清掉无效锚点
    if (location.hash === "#login") {
      history.replaceState(null, "", location.pathname + location.search);
      openLoginModal();
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    bind();
    bindLogin();
    loadOverview().catch((err) => toast("内容加载失败", err.message, "error"));
  });
})();
