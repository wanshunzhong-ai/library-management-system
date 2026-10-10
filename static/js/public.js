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
    el.innerHTML = `<div class="t-title">${esc(title)}</div>` + (body ? `<div class="t-body">${esc(body)}</div>` : "");
    layer.appendChild(el);
    setTimeout(() => {
      el.style.transition = "opacity .2s, transform .2s";
      el.style.opacity = "0";
      el.style.transform = "translateX(16px)";
      setTimeout(() => el.remove(), 220);
    }, kind === "error" ? 5000 : 2600);
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
    const badge = b.available
      ? `<span class="badge badge-ok">${esc(b.status_label)}</span>`
      : `<span class="badge badge-danger">全部借出</span>`;
    return `<article class="book-card">
      <div class="bc-top">
        <div>
          <div class="bc-title">${esc(b.title)}</div>
          <div class="bc-author">${esc(b.author || "佚名")}</div>
        </div>
        ${badge}
      </div>
      <div class="bc-meta">
        <span>${esc(b.category)}</span>
        <span>${esc(b.publisher || "—")}</span>
        ${b.publish_year ? `<span>${esc(b.publish_year)}</span>` : ""}
      </div>
      <div class="bc-foot">
        <span class="shelf">馆藏位置 <b>${esc(b.location || "待分配")}</b></span>
        <span class="badge badge-muted">${Number(b.available_copies) || 0}/${Number(b.total_copies) || 0} 册</span>
      </div>
    </article>`;
  }

  function renderCards(container, list, emptyText) {
    container.innerHTML = list.length
      ? list.map(bookCard).join("")
      : `<div class="empty-state"><p>${esc(emptyText)}</p></div>`;
  }

  function renderRules(rules) {
    const cards = [
      { ico: "📖", v: rules.loan_days, u: "天", k: "单次借期", d: "自借出之日起计算" },
      { ico: "🔁", v: rules.max_renew, u: "次", k: "可续借次数", d: "每次续借顺延一个借期" },
      { ico: "💰", v: money(rules.fine_per_day), u: "", k: "逾期罚金", d: "按逾期天数累计" },
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
        <th>书名 / 作者</th><th>出版社</th><th>分类</th><th>馆藏位置</th><th>在架状态</th><th>馆藏册数</th>
      </tr></thead>
      <tbody>${payload.items
        .map((b) => {
          const badge = b.available
            ? `<span class="badge ${Number(b.available_copies) === 1 ? "badge-warn" : "badge-ok"}">${esc(b.status_label)}</span>`
            : `<span class="badge badge-danger">全部借出</span>`;
          return `<tr>
            <td><strong>${esc(b.title)}</strong><div class="sub">${esc(b.author || "佚名")} · ${esc(b.isbn)}</div></td>
            <td>${esc(b.publisher || "—")}${b.publish_year ? `<div class="sub">${esc(b.publish_year)} 年</div>` : ""}</td>
            <td><span class="badge badge-muted">${esc(b.category)}</span></td>
            <td class="mono">${esc(b.location || "待分配")}</td>
            <td>${badge}</td>
            <td class="mono">${Number(b.available_copies) || 0} / ${Number(b.total_copies) || 0}</td>
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
  }

  document.addEventListener("DOMContentLoaded", () => {
    bind();
    loadOverview().catch((err) => toast("内容加载失败", err.message, "error"));
  });
})();
