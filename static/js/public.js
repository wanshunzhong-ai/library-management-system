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

  /* 星级：门户只展示已发布评价的聚合（口径由后端统一给出） */
  const stars = (avg, count, size) => {
    const value = Number(avg) || 0;
    const cls = "stars" + (size === "sm" ? " stars-sm" : "");
    const full = Math.round(value);
    let html = `<span class="${cls}" role="img" aria-label="平均 ${value.toFixed(1)} 分，共 ${count} 条评价">`;
    for (let i = 1; i <= 5; i += 1) {
      html += `<i class="${i <= full ? "on" : ""}" aria-hidden="true">★</i>`;
    }
    html += "</span>";
    return html;
  };

  /* 卡片/表格里的一行评分：没有评价时不显示，避免满屏「0 分」造成误解 */
  const ratingInline = (b) => {
    const count = Number(b.rating_count) || 0;
    if (!count) return "";
    const avg = Number(b.rating_avg) || 0;
    return `${stars(avg, count, "sm")}<span class="rating-num">${avg.toFixed(1)}</span><span class="rating-cnt">（${count} 条）</span>`;
  };

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

  const state = { keyword: "", category: "", location: "", availableOnly: false, page: 1, pageSize: 8 };
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
      ${Number(b.rating_count) > 0 ? `<div class="bc-rating">${ratingInline(b)}</div>` : ""}
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
          <div><span>读者评分</span><b>${Number(b.rating_count) > 0 ? ratingInline(b) : '<span class="sub">暂无评价</span>'}</b></div>
        </div>
        <div class="bk-reviews" id="bkReviews">
          <div class="bk-reviews-head">读者评价</div>
          <div class="bk-reviews-body" id="bkReviewsBody"><div class="rv-hint">正在加载评价…</div></div>
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
    loadBookReviews(book.id);
  }

  /** 详情弹窗里的评价列表：免登录只读，仅展示已发布评价（作者按「姓 + 星号」脱敏） */
  async function loadBookReviews(bookId) {
    const box = $("#bkReviewsBody");
    if (!box) return;
    try {
      const data = await getJSON(`/api/public/books/${encodeURIComponent(bookId)}/reviews`, {
        page: 1,
        page_size: 5,
      });
      const summary = data.summary || { avg: 0, total: 0 };
      const items = data.items || [];
      const head = `<div class="rv-summary">
        ${summary.total ? `${stars(summary.avg, summary.total)}<b>${Number(summary.avg).toFixed(1)}</b>
        <span class="rv-cnt">共 ${summary.total} 条评价</span>` : '<span class="rv-hint">这本书还没有读者评价。登录后可对你借阅过的图书打分。</span>'}
      </div>`;
      if (!items.length) {
        box.innerHTML = head;
        return;
      }
      box.innerHTML =
        head +
        items
          .map(
            (r) => `<div class="rv-item">
              <div class="rv-top">
                <span class="rv-who">${esc(r.member_display || "读者")}</span>
                ${stars(r.rating, 1, "sm")}
                <span class="rv-time">${esc(String(r.updated_at || r.created_at || "").slice(0, 10))}</span>
              </div>
              ${r.content ? `<p class="rv-text">${esc(r.content)}</p>` : '<p class="rv-text is-empty">（未填写文字评价）</p>'}
            </div>`
          )
          .join("");
    } catch (err) {
      box.innerHTML = '<div class="rv-hint">评价加载失败，可稍后重试。</div>';
    }
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

  /* ---------------- 公告 / 开馆时间 ---------------- */

  function renderNotices(items) {
    const box = $("#noticeList");
    if (!box) return;
    const list = items || [];
    const summary = $("#noticeSummary");
    if (summary) summary.textContent = list.length ? `共 ${list.length} 条` : "暂无公告";
    if (!list.length) {
      box.innerHTML = `<div class="empty-state"><p>暂无公告</p><small>开馆安排与临时通知会显示在这里</small></div>`;
      return;
    }
    box.innerHTML = list
      .map((a) => {
        const tone = a.level === "important" ? " is-important" : "";
        const pin = a.pinned ? `<span class="badge badge-warn">置顶</span>` : "";
        const kind = a.kind === "hours" ? `<span class="badge badge-muted">开馆时间</span>` : "";
        return `<article class="notice${tone}">
          <div class="notice-head">
            ${pin}${kind}
            <h3>${esc(a.title)}</h3>
            <span class="notice-time">${esc(a.window_label || "")}</span>
          </div>
          ${a.body ? `<p class="notice-body">${esc(a.body)}</p>` : ""}
        </article>`;
      })
      .join("");
  }

  /** 开馆时间：门户「关于本馆」里由馆员维护的那条 hours 公告覆盖静态文案 */
  function renderHours(hours) {
    const node = $("#aboutHours");
    if (!node || !hours) return;
    node.textContent = hours.body || hours.title || node.textContent;
  }

  /* ---------------- 馆藏分布 ---------------- */

  function renderLocations(items) {
    const box = $("#locationGrid");
    if (!box) return;
    const list = items || [];
    if (!list.length) {
      box.innerHTML = `<div class="empty-state"><p>暂无位置信息</p><small>馆员为图书登记馆藏位置后即可按架位浏览</small></div>`;
      return;
    }
    box.innerHTML = list
      .map((loc) => {
        const name = loc.pending ? "待分配" : loc.location;
        const cats = (loc.categories || [])
          .slice(0, 3)
          .map((c) => `<span class="loc-cat">${esc(c)}</span>`)
          .join("");
        const avail = Number(loc.available_copies) || 0;
        return `<button type="button" class="loc-card${avail > 0 ? " is-ok" : ""}"
                  data-location="${esc(loc.pending ? "__none__" : loc.location)}"
                  aria-label="查看位置「${esc(name)}」的全部图书">
          <div class="loc-name">${esc(name)}</div>
          <div class="loc-v">${avail}<small>册可借</small></div>
          <div class="loc-sub">共 ${Number(loc.titles) || 0} 种 · ${Number(loc.copies) || 0} 册</div>
          <div class="loc-cats">${cats}</div>
        </button>`;
      })
      .join("");
  }

  /* ---------------- 在线办证 / 借阅速查 ---------------- */

  function fillSelect(select, values) {
    if (!select) return;
    select.innerHTML = (values || []).map((v) => `<option value="${esc(v)}">${esc(v)}</option>`).join("");
  }

  function renderApplyOptions(options) {
    const opt = options || {};
    fillSelect($("#applyMemberType"), opt.member_types || []);
    fillSelect($("#applyIdType"), opt.id_types || []);
  }

  function pkError(node, message) {
    if (!node) return;
    if (!message) {
      node.hidden = true;
      node.textContent = "";
      return;
    }
    node.hidden = false;
    node.textContent = message;
  }

  /** 提交结果 / 速查结果的统一渲染（不插入任何后端未提供的字段） */
  function pkResult(node, html) {
    if (!node) return;
    node.hidden = !html;
    node.innerHTML = html || "";
  }

  function openModal(id) {
    const layer = $(id);
    if (!layer) return;
    layer.hidden = false;
    const first = layer.querySelector(".pk-tab.is-active") || layer.querySelector("input, select, textarea, button");
    if (first && first.focus) {
      try {
        first.focus({ preventScroll: true });
      } catch (e) {
        /* 忽略：无法聚焦不影响使用 */
      }
    }
  }

  function closeModal(id) {
    const layer = $(id);
    if (!layer) return;
    layer.hidden = true;
    layer.querySelectorAll("input:not([type=hidden]), textarea").forEach((el) => {
      if (el.dataset.keep !== "1") el.value = "";
    });
    layer.querySelectorAll(".pk-error").forEach((el) => pkError(el, ""));
  }

  function postJSON(path, body) {
    return fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body),
    }).then(async (response) => {
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
        const err = (payload && payload.error) || {};
        const e = new Error(err.message || `请求失败（HTTP ${response.status}）`);
        e.field = err.details && err.details.field;
        throw e;
      }
      return payload;
    });
  }

  function switchApplyTab(name) {
    document.querySelectorAll(".pk-tab").forEach((t) => t.classList.toggle("is-active", t.dataset.pkTab === name));
    $("#applyForm").hidden = name !== "form";
    $("#lookupForm").hidden = name !== "lookup";
    pkError($("#applyError"), "");
    pkError($("#lookupError"), "");
    pkResult($("#applyResult"), "");
  }

  async function submitApplication(event) {
    event.preventDefault();
    const btn = $("#applySubmitBtn");
    const payload = {
      name: $("#applyName").value.trim(),
      phone: $("#applyPhone").value.trim(),
      email: $("#applyEmail").value.trim(),
      member_type: $("#applyMemberType").value,
      id_type: $("#applyIdType").value,
      id_tail: $("#applyIdTail").value.trim(),
      note: $("#applyNote").value.trim(),
    };
    if (!payload.name) return pkError($("#applyError"), "请填写姓名");
    if (!payload.phone) return pkError($("#applyError"), "请填写手机号");

    btn.disabled = true;
    const original = btn.textContent;
    btn.textContent = "提交中…";
    try {
      const result = await postJSON("/api/public/member-applications", payload);
      pkError($("#applyError"), "");
      pkResult(
        $("#applyResult"),
        `<div class="pk-ok">
           <b>申请已提交</b>
           <p>请记下申请编号：<span class="pk-code">${esc(result.code)}</span></p>
           <p>馆员核验通过后，携带本人证件到馆领取读者证；可切到「查询进度」随时查看状态。</p>
         </div>`
      );
      $("#applyForm").reset();
      toast("办证申请已提交", `申请编号 ${result.code}，请妥善保存`, "ok");
    } catch (err) {
      pkError($("#applyError"), err.message);
    } finally {
      btn.disabled = false;
      btn.textContent = original;
    }
  }

  async function lookupApplication(event) {
    event.preventDefault();
    const btn = $("#lookupSubmitBtn");
    const code = $("#lookupCode").value.trim();
    const phone = $("#lookupPhone").value.trim();
    if (!code) return pkError($("#lookupError"), "请填写申请编号");
    if (!phone) return pkError($("#lookupError"), "请填写手机号");

    btn.disabled = true;
    const original = btn.textContent;
    btn.textContent = "查询中…";
    try {
      const result = await postJSON("/api/public/member-applications/lookup", { code, phone });
      pkError($("#lookupError"), "");
      const extra = result.card_no
        ? `<p>读者证号：<span class="pk-code">${esc(result.card_no)}</span></p>`
        : "";
      const reason = result.reject_reason
        ? `<p>驳回原因：${esc(result.reject_reason)}</p>`
        : "";
      pkResult(
        $("#applyResult"),
        `<div class="pk-ok">
           <b>${esc(result.name)} · ${esc(result.status_label)}</b>
           <p>申请编号 <span class="pk-code">${esc(result.code)}</span>，提交于 ${esc(result.created_at)}</p>
           ${extra}${reason}
           <p class="pk-hint">${esc(result.hint || "")}</p>
         </div>`
      );
    } catch (err) {
      pkError($("#lookupError"), err.message);
    } finally {
      btn.disabled = false;
      btn.textContent = original;
    }
  }

  async function runQuickCheck(event) {
    event.preventDefault();
    const btn = $("#quickSubmitBtn");
    const account = $("#quickAccount").value.trim();
    const password = $("#quickPassword").value;
    if (!account) return pkError($("#quickError"), "请输入借书证号或手机号");
    if (!password) return pkError($("#quickError"), "请输入密码");

    btn.disabled = true;
    const original = btn.textContent;
    btn.textContent = "查询中…";
    try {
      const data = await postJSON("/api/public/quick-check", { account, password });
      pkError($("#quickError"), "");
      pkResult($("#quickResult"), quickResultHtml(data));
    } catch (err) {
      pkResult($("#quickResult"), "");
      pkError($("#quickError"), err.message);
    } finally {
      btn.disabled = false;
      btn.textContent = original;
    }
  }

  function quickResultHtml(data) {
    const m = data.member || {};
    const s = data.summary || {};
    const items = data.items || [];
    const rows = items.length
      ? items
          .map(
            (i) => `<tr>
              <td>${esc(i.title)}</td>
              <td class="mono">${esc(String(i.due_at || "").slice(0, 10))}</td>
              <td>${
                i.overdue
                  ? `<span class="badge badge-danger">逾期 ${esc(i.overdue_days)} 天 · ${money(i.fine)}</span>`
                  : `<span class="badge badge-ok">在借</span>`
              }</td>
            </tr>`
          )
          .join("")
      : `<tr><td colspan="3" class="pk-empty">当前没有在借图书</td></tr>`;
    return `<div class="pk-ok">
        <b>${esc(m.name)} · ${esc(m.card_no)}</b>
        <p>${esc(m.member_type)} · ${esc(m.status_label)}${
          m.card_lost ? ' · <span class="badge badge-danger">借书证已挂失</span>' : ""
        }</p>
      </div>
      <div class="pk-stats">
        <div><b>${esc(s.active_loans)}</b><span>在借册数</span></div>
        <div><b>${esc(s.overdue_loans)}</b><span>逾期册数</span></div>
        <div><b>${money(s.fine_outstanding)}</b><span>待缴滞纳金</span></div>
        <div><b>${esc(s.reservation_ready)}</b><span>已到书预约</span></div>
      </div>
      <div class="pk-next">最近应还：${
        data.next_due_at ? esc(String(data.next_due_at).slice(0, 10)) : "—"
      }　剩余可借 ${esc(s.quota_left)} / ${esc(s.max_borrow)} 册</div>
      <div class="table-scroll pk-table-wrap"><table class="table pk-table">
        <thead><tr><th>书名</th><th>应还日期</th><th>状态</th></tr></thead>
        <tbody>${rows}</tbody>
      </table></div>
      <p class="pk-hint">仅供快速查看，如需续借 / 缴纳滞纳金 / 预约图书，请登录门户进入「我的借阅」。</p>`;
  }

  async function loadOverview() {
    const data = await getJSON("/api/public/overview");
    renderStats(data.stats || {});
    renderCategoryOptions(data.categories || []);
    renderChips(data.categories || []);
    renderNotices(data.announcements || []);
    renderHours(data.hours || null);
    renderLocations(data.locations || []);
    renderApplyOptions(data.application_options || {});
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
        <th>书名 / 作者</th><th>馆藏条码</th><th>出版社</th><th>分类</th><th>馆藏位置</th><th>在架状态</th><th>馆藏册数</th><th>评分</th><th>详情</th>
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
            <td>${Number(b.rating_count) > 0 ? ratingInline(b) : '<span class="sub">—</span>'}</td>
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
        location: state.location,
        available_only: state.availableOnly ? "1" : "",
        page: state.page,
        page_size: state.pageSize,
      });
      searchStarted = true;
      renderSearchTable(payload);
      const parts = [];
      if (payload.keyword) parts.push(`关键词「${payload.keyword}」`);
      if (state.category) parts.push(`分类「${state.category}」`);
      if (state.location) {
        parts.push(`位置「${state.location === "__none__" ? "待分配" : state.location}」`);
      }
      if (state.availableOnly) parts.push("仅可借");
      summary.textContent = `共 ${payload.total} 条${parts.length ? " · " + parts.join(" · ") : ""}`;
      const clear = $("#clearLocationBtn");
      if (clear) clear.hidden = !state.location;
      $("#search").scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (err) {
      summary.textContent = "检索失败";
      toast("检索失败", err.message, "error");
    }
  }

  /** 点某个馆藏位置 → 把检索限定到该位置（再点一次可清除） */
  function filterByLocation(location) {
    if (state.location === location) {
      state.location = "";
    } else {
      state.location = location;
      state.keyword = "";
      state.page = 1;
      const q = $("#q");
      if (q) q.value = "";
    }
    state.page = 1;
    runSearch();
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

  /** 弹窗的 Tab 焦点陷阱：焦点只在弹窗内循环（三个弹窗共用） */
  function trapLoginTab(event, layerSelector) {
    const layer = $(layerSelector || "#loginModal");
    if (!layer) return;
    const focusables = Array.from(layer.querySelectorAll("button, input, a[href]")).filter(
      (el) => !el.disabled && el.offsetParent !== null
    );
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
      else if (!$("#applyModal").hidden) closeModal("#applyModal");
      else if (!$("#quickModal").hidden) closeModal("#quickModal");
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

    // 馆藏分布：点位置卡片把检索限定到该位置
    const locGrid = $("#locationGrid");
    if (locGrid) {
      locGrid.addEventListener("click", (e) => {
        const card = e.target.closest("[data-location]");
        if (card) filterByLocation(card.dataset.location);
      });
    }
    const clearLoc = $("#clearLocationBtn");
    if (clearLoc) {
      clearLoc.addEventListener("click", () => {
        state.location = "";
        state.page = 1;
        runSearch();
      });
    }

    // 在线办证
    const openApply = () => {
      switchApplyTab("form");
      openModal("#applyModal");
    };
    ["#applyCardBtn", "#heroApplyBtn", "#aboutApplyBtn"].forEach((sel) => {
      const btn = $(sel);
      if (btn) btn.addEventListener("click", openApply);
    });
    const applyClose = $("#applyModalClose");
    if (applyClose) applyClose.addEventListener("click", () => closeModal("#applyModal"));
    const applyLayer = $("#applyModal");
    if (applyLayer) {
      applyLayer.addEventListener("click", (e) => {
        if (e.target === applyLayer) closeModal("#applyModal");
      });
      applyLayer.addEventListener("keydown", (e) => {
        if (e.key === "Tab") trapLoginTab(e, "#applyModal");
      });
    }
    document.querySelectorAll(".pk-tab").forEach((tab) =>
      tab.addEventListener("click", () => switchApplyTab(tab.dataset.pkTab))
    );
    const applyForm = $("#applyForm");
    if (applyForm) applyForm.addEventListener("submit", submitApplication);
    const lookupForm = $("#lookupForm");
    if (lookupForm) lookupForm.addEventListener("submit", lookupApplication);

    // 借阅速查
    ["#quickCheckBtn", "#heroQuickBtn"].forEach((sel) => {
      const btn = $(sel);
      if (btn) btn.addEventListener("click", () => openModal("#quickModal"));
    });
    const quickClose = $("#quickModalClose");
    if (quickClose) quickClose.addEventListener("click", () => closeModal("#quickModal"));
    const quickLayer = $("#quickModal");
    if (quickLayer) {
      quickLayer.addEventListener("click", (e) => {
        if (e.target === quickLayer) closeModal("#quickModal");
      });
      quickLayer.addEventListener("keydown", (e) => {
        if (e.key === "Tab") trapLoginTab(e, "#quickModal");
      });
    }
    const quickForm = $("#quickForm");
    if (quickForm) quickForm.addEventListener("submit", runQuickCheck);
    const quickToggle = $("#quickPwToggle");
    if (quickToggle) {
      quickToggle.addEventListener("click", () => {
        const pw = $("#quickPassword");
        const showing = pw.type === "text";
        pw.type = showing ? "password" : "text";
        quickToggle.textContent = showing ? "显示" : "隐藏";
      });
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    bind();
    bindLogin();
    loadOverview().catch((err) => toast("内容加载失败", err.message, "error"));
  });
})();
