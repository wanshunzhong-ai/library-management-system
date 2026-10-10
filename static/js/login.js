/* 管理员登录 */
(function () {
  "use strict";

  const $ = (sel) => document.querySelector(sel);

  function setError(message, field) {
    const box = $("#loginError");
    if (!message) {
      box.hidden = true;
      box.textContent = "";
    } else {
      box.hidden = false;
      box.textContent = message;
    }
    ["#fieldUsername", "#fieldPassword"].forEach((sel) => $(sel).classList.remove("has-error"));
    if (field === "username") $("#fieldUsername").classList.add("has-error");
    if (field === "password") $("#fieldPassword").classList.add("has-error");
  }

  function safeNext() {
    const raw = new URLSearchParams(location.search).get("next") || "/admin";
    // 只允许站内相对路径，避免开放重定向
    if (!raw.startsWith("/") || raw.startsWith("//")) return "/admin";
    return raw;
  }

  document.addEventListener("DOMContentLoaded", () => {
    const form = $("#loginForm");
    const btn = $("#loginBtn");
    const pw = $("#password");

    $("#pwToggle").addEventListener("click", () => {
      const showing = pw.type === "text";
      pw.type = showing ? "password" : "text";
      $("#pwToggle").textContent = showing ? "显示" : "隐藏";
    });

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      setError("");

      const username = $("#username").value.trim();
      const password = pw.value;
      if (!username) return setError("请输入用户名", "username");
      if (!password) return setError("请输入密码", "password");

      btn.disabled = true;
      const original = btn.textContent;
      btn.textContent = "登录中…";

      try {
        const response = await fetch("/api/auth/login", {
          method: "POST",
          headers: { "Content-Type": "application/json", Accept: "application/json" },
          body: JSON.stringify({ username, password }),
        });
        const text = await response.text();
        let payload = null;
        if (text) { try { payload = JSON.parse(text); } catch (err) { payload = null; } }

        if (!response.ok) {
          const errObj = (payload && payload.error) || {};
          setError(errObj.message || `登录失败（HTTP ${response.status}）`, errObj.details && errObj.details.field);
          btn.disabled = false;
          btn.textContent = original;
          return;
        }

        btn.textContent = "登录成功，正在进入…";
        location.replace(safeNext());
      } catch (err) {
        setError("无法连接服务器，请确认服务是否在运行");
        btn.disabled = false;
        btn.textContent = original;
      }
    });
  });
})();
