// 登录页：提交访问令牌，服务端写入 HttpOnly Cookie 后进入工作台。
"use strict";

(function () {
  const form = document.getElementById("login-form");
  const input = document.getElementById("login-token");
  const message = document.getElementById("login-msg");
  const submit = document.getElementById("login-submit");

  function show(text) {
    message.textContent = text;
    message.classList.remove("hidden");
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!input.value.trim()) {
      show("请输入访问令牌。");
      input.focus();
      return;
    }
    submit.disabled = true;
    try {
      const resp = await fetch("/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: input.value }),
      });
      if (resp.ok) {
        location.replace("/");
        return;
      }
      let detail = "登录失败，请稍后重试。";
      try { detail = (await resp.json()).detail || detail; } catch (_) { /* 非 JSON 错误 */ }
      show(detail);
      input.select();
    } catch (_) {
      show("连接服务失败，请检查服务是否在运行。");
    } finally {
      submit.disabled = false;
    }
  });
})();
