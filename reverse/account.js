"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const panel = document.createElement("dialog");
  panel.id = "account-dialog";
  panel.setAttribute("aria-labelledby", "account-title");
  panel.innerHTML = `<header class="panel-header"><h2 id="account-title">你的账户</h2><button class="icon-button" id="account-close" aria-label="关闭账户面板">×</button></header>
 <div class="panel-body"><p id="account-status" class="panel-status" role="status" aria-live="polite"></p>
 <section id="auth-section"><nav class="panel-tabs" aria-label="登录方式"><button data-auth="login" class="selected">登录</button><button data-auth="register" hidden>注册</button><button data-auth="reset">重置密码</button></nav>
 <form id="auth-form"><label id="username-label">用户名<input id="auth-username" name="username" autocomplete="username" minlength="2" maxlength="24" required></label>
 <label id="reset-label" hidden>重置凭据<input id="auth-reset" name="reset-token" autocomplete="off" maxlength="100"></label>
 <label>密码<input id="auth-password" name="password" type="password" autocomplete="current-password" required maxlength="128"></label>
 <p id="auth-note" class="subtle">用户名与密码登录。忘记密码时，请联系管理员获取重置凭据。</p><button class="button primary" id="auth-submit">登录</button></form></section>
 <section id="member-section" hidden><div class="wallet-heading"><div><p id="member-name"></p><strong id="wallet-available">0</strong><span> 可用小鱼干</span></div><p id="wallet-held" class="subtle"></p></div>
 <nav class="panel-tabs" aria-label="账户功能"><button data-pane="redeem" class="selected">兑换</button><button data-pane="ledger">小鱼干明细</button><button data-pane="jobs">修复记录</button><button data-pane="security">设置</button></nav>
 <section id="pane-redeem"><form id="redeem-form"><label>兑换卡密<input id="redeem-code" autocomplete="off" placeholder="PR-…" maxlength="100" required></label><p class="subtle">每张卡密仅可兑换一次，兑换后小鱼干不过期。</p><button class="button primary">兑换小鱼干</button></form></section>
 <section id="pane-ledger" hidden><div id="ledger-list" class="record-list"></div></section>
 <section id="pane-jobs" hidden><p class="subtle">照片保留 30 天，之后仅保留修复与消费记录。</p><div id="jobs-list" class="record-list"></div></section>
 <div id="account-pagination" class="pagination" hidden><button class="text-button" id="account-prev">上一页</button><button class="text-button" id="account-next">下一页</button></div>
 <section id="pane-security" hidden><form id="password-form"><label>当前密码<input id="password-current" type="password" autocomplete="current-password" required maxlength="128"></label><label>新密码<input id="password-new" type="password" autocomplete="new-password" minlength="10" maxlength="128" required></label><p class="subtle">修改后需重新登录，所有旧会话都会退出。</p><button class="button secondary">修改密码</button></form></section>
 <div class="member-footer"><a id="admin-link" class="text-button" href="/admin" hidden>管理后台 ↗</a><button id="account-logout" class="text-button">退出登录</button></div></section></div>`;
  document.body.append(panel);
  if (window.location.hostname === "save-pic.naix.top")
    $("admin-link").href = "https://admin-save-pic.naix.top/";
  const motion = UI.dialog(panel, { backdropClose: true });
  let lifecycle = 0, accountRevision = 0, identityRevision = 0;
  let user = null,
    config = { prices: { light: 1, fine: 5 }, welcome: 5, version: 0, webRegistrationEnabled: false },
    csrf = "",
    authMode = "login",
    pane = "redeem",
    offset = 0,
    viewRevision = 0;
  const status = (text = "", error = false) => {
    $("account-status").textContent = text;
    $("account-status").classList.toggle("error", error);
  };
  const element = (tag, text, className) => {
    const e = document.createElement(tag);
    e.textContent = text;
    if (className) e.className = className;
    return e;
  };
  const remaining = (value) => {
    const minutes = Math.max(1, Math.ceil((value - Date.now()) / 60000));
    return `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分钟`;
  };
  const time = (value) =>
    new Date(value).toLocaleString("zh-CN", { hour12: false });
  function assign(data) {
    accountRevision++;
    const previousId = user?.id || null;
    if ("user" in data) user = data.user;
    if (previousId !== (user?.id || null)) {
      identityRevision++;
      viewRevision++;
      $("ledger-list").replaceChildren();
      $("jobs-list").replaceChildren();
      $("member-name").textContent = "";
      $("wallet-available").textContent = "0";
      $("wallet-held").textContent = "";
    }
    if (data.csrfToken) csrf = data.csrfToken;
    if (data.config) config = data.config;
    render();
    window.dispatchEvent(
      new CustomEvent("accountchange", { detail: { user, previousId } }),
    );
  }
  function render() {
    const canRegister = config.webRegistrationEnabled === true;
    $("auth-section").querySelector('[data-auth="register"]').hidden = !canRegister;
    if (!canRegister && authMode === "register") selectAuth("login");
    const toggle = $("account-toggle");
    if (toggle) {
      toggle.replaceChildren();
      if (user) {
        toggle.append(element("span", user.username, "account-name"), element("span", `${user.available} 小鱼干`, "account-balance"));
      } else toggle.textContent = canRegister ? "登录 / 注册" : "登录";
    }
    $("auth-section").hidden = !!user;
    $("member-section").hidden = !user;
    $("account-title").textContent = user ? "你的账户" : canRegister ? "登录与注册" : "账户登录";
    if (user) {
      $("member-name").textContent = user.username;
      $("wallet-available").textContent = user.available;
      $("wallet-held").textContent = `处理中冻结 ${user.held} 小鱼干`;
      $("admin-link").hidden = user.role !== "admin";
    }
    $("auth-note").textContent =
      authMode === "register"
        ? `注册赠送 ${config.welcome} 小鱼干。密码须为 10 至 128 个字符。`
        : authMode === "reset"
          ? "凭据由管理员提供，30 分钟内有效且仅可使用一次。"
          : canRegister
            ? "用户名与密码登录。忘记密码时，请联系管理员获取重置凭据。"
            : "新账号请在微信小程序「猫猫九命机」注册。已有网页账号可继续登录。";
    for (const b of document.querySelectorAll("[data-quality]"))
      b.textContent = `${b.dataset.quality === "light" ? "轻量" : "精细"} · ${config.prices[b.dataset.quality]} 小鱼干`;
  }
  async function refresh() {
    const own = accountRevision;
    const res = await fetch("/api/auth/session", {
      credentials: "same-origin",
    });
    if (!res.ok) throw new Error("无法连接账户服务。");
    const data = await res.json();
    if (own === accountRevision) assign(data);
    return data;
  }
  const ready = refresh().catch(() => {
    status("账户服务暂时无法连接，请稍后刷新。", true);
    render();
  });
  async function request(path, options = {}) {
    await ready;
    const owner = identityRevision,
      own = accountRevision,
      method = options.method || "GET",
      headers = { ...options.headers };
    if (method !== "GET") headers["X-CSRF-Token"] = csrf;
    let body = options.body;
    if (body && !(body instanceof FormData)) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(body);
    }
    const res = await fetch(path, {
      ...options,
      method,
      headers,
      body,
      credentials: "same-origin",
    });
    const checkOwner = () => {
      if (owner !== identityRevision)
        throw new Error("账户已变更，请重新查看。");
    };
    checkOwner();
    if (!res.ok) {
      const data = await res.json().catch(() => null),
        error = new Error(data?.error?.message || "操作未完成，请稍后重试。");
      checkOwner();
      error.code = data?.error?.code;
      error.status = res.status;
      if (error.code === "UNAUTHORIZED" || error.code === "CSRF")
        await refresh().catch(() => {});
      throw error;
    }
    if (options.binary) return res;
    const data = await res.json();
    checkOwner();
    // Reads started before a completed mutation must not roll its wallet back.
    if (method === "GET" && own !== accountRevision) {
      if ("user" in data) data.user = user;
      if ("config" in data) data.config = config;
    }
    return data;
  }
  async function refreshBalance() {
    if (!user) return;
    const own = accountRevision;
    const data = await request("/api/account");
    if (own === accountRevision) assign(data);
  }
  function open(which = "redeem") {
    if (user) {
      pane = which;
      refreshBalance().catch(() => {});
    }
    status();
    render();
    if (!panel.open || motion.closing) { lifecycle++; motion.open(); }
    if (user) showPane(pane);
    else {
      selectAuth("login");
      $("auth-username").focus();
    }
  }
  function selectAuth(mode) {
    if (mode === "register" && config.webRegistrationEnabled !== true) return;
    if (authMode !== mode) lifecycle++;
    authMode = mode;
    $("username-label").hidden = mode === "reset";
    $("reset-label").hidden = mode !== "reset";
    $("auth-username").required = mode !== "reset";
    $("auth-reset").required = mode === "reset";
    $("auth-password").minLength = mode === "login" ? 1 : 10;
    $("auth-password").autocomplete =
      mode === "login" ? "current-password" : "new-password";
    $("auth-submit").textContent = {
      login: "登录",
      register: "注册并领取小鱼干",
      reset: "重置密码",
    }[mode];
    authTabs.select(panel.querySelector(`[data-auth="${mode}"]`));
    status();
    render();
  }
  async function showPane(name, newOffset = 0) {
    lifecycle++;
    pane = name;
    offset = newOffset;
    const own = ++viewRevision,
      owner = user?.id;
    for (const p of ["redeem", "ledger", "jobs", "security"])
      $("pane-" + p).hidden = p !== name;
    memberTabs.select(panel.querySelector(`[data-pane="${name}"]`));
    $("account-pagination").hidden = !["ledger", "jobs"].includes(name);
    status();
    if (!["ledger", "jobs"].includes(name)) return;
    const target = $(name + "-list");
    UI.loading(target);
    $("account-prev").disabled = true;
    $("account-next").disabled = true;
    try {
      const data = await request(`/api/${name}?offset=${offset}`);
      if (own !== viewRevision || user?.id !== owner) return;
      target.removeAttribute("aria-busy");
      target.replaceChildren();
      for (const item of data.items) {
        const row = element("div", "", "record");
        if (name === "ledger") {
          row.append(
            element("strong", item.reason),
            element("small", time(item.created_at)),
          );
          const delta = item.available_delta;
          row.append(
            element(
              "p",
              `可用 ${delta >= 0 ? "+" : ""}${delta} · 冻结 ${item.held_delta >= 0 ? "+" : ""}${item.held_delta}　余额 ${item.available_after}`,
            ),
          );
        } else {
          row.append(
            element(
              "strong",
              `${item.quality === "light" ? "轻量" : "精细"}修复 · ${item.cost} 小鱼干`,
            ),
            element("small", time(item.createdAt)),
          );
          const description = {
            running: "正在修复，关页后仍会继续",
            failed:
              (item.error?.message || "修复失败，小鱼干已退回") +
              (item.expiresAt && item.expiresAt <= Date.now()
                ? " 照片已过期删除。"
                : ""),
            expired: "照片已超过保留期限，已删除",
            succeeded: `剩余 ${remaining(item.expiresAt)} · ${time(item.expiresAt)} 到期`,
          };
          row.append(element("p", description[item.status]));
          if (
            ["running", "succeeded"].includes(item.status) ||
            (item.status === "failed" && item.expiresAt > Date.now())
          ) {
            const b = element(
              "button",
              item.status === "running" ? "继续查看" : "查看照片",
              "text-button",
            );
            b.type = "button";
            b.onclick = () => {
              motion.close({ immediate: true, restoreFocus: false });
              window.dispatchEvent(
                new CustomEvent("restorejob", { detail: item.id }),
              );
            };
            row.append(b);
          }
        }
        target.append(row);
      }
      if (!data.items.length)
        target.append(element("p", "暂无记录。", "subtle"));
      $("account-prev").disabled = offset === 0;
      $("account-next").disabled = data.items.length < 50;
    } catch (error) {
      if (own === viewRevision) UI.loadError(target, error.message, () => showPane(name, newOffset));
    }
  }
  async function submit(form, fn) {
    if (form.getAttribute("aria-busy") === "true") return;
    const own = lifecycle;
    const inputs = [...form.querySelectorAll("input")];
    inputs.forEach(e => e.disabled = true);
    UI.busy(form, true);
    status();
    try { await fn(() => own === lifecycle); }
    catch (error) { if (own === lifecycle) status(error.message, true); }
    finally {
      inputs.forEach(e => e.disabled = false);
      UI.busy(form, false);
      if (form.id === "auth-form") $("auth-submit").textContent = { login: "登录", register: "注册并领取小鱼干", reset: "重置密码" }[authMode];
    }
  }
  const authTabs = UI.tabs(panel.querySelector('[aria-label="登录方式"]'), '[data-auth]', () => $("auth-form"), b => selectAuth(b.dataset.auth));
  const memberTabs = UI.tabs(panel.querySelector('[aria-label="账户功能"]'), '[data-pane]', b => $("pane-" + b.dataset.pane), b => showPane(b.dataset.pane));
  $("account-toggle")?.addEventListener("click", () => open());
  $("account-close").onclick = () => motion.close();
  panel.addEventListener("uiclose", () => {
    lifecycle++;
    viewRevision++;
    for (const id of ["auth-password", "auth-reset", "redeem-code", "password-current", "password-new"]) $(id).value = "";
  });
  $("auth-form").onsubmit = (e) => {
    e.preventDefault();
    if (authMode === "register" && config.webRegistrationEnabled !== true) return;
    const body =
      authMode === "reset"
        ? {
            token: $("auth-reset").value.trim(),
            newPassword: $("auth-password").value,
          }
        : {
            username: $("auth-username").value,
            password: $("auth-password").value,
          };
    const mode = authMode;
    submit(e.target, async isCurrent => {
      const data = await request("/api/auth/" + mode, { method: "POST", body });
      assign(data);
      if (!isCurrent()) return;
      $("auth-password").value = "";
      if (mode === "reset") {
        selectAuth("login");
        status("密码已重置，请登录。");
      } else motion.close();
    });
  };
  $("redeem-form").onsubmit = (e) => {
    e.preventDefault();
    const code = $("redeem-code").value;
    submit(e.target, async isCurrent => {
      const data = await request("/api/cards/redeem", {
        method: "POST",
        body: { code },
      });
      assign(data);
      if (!isCurrent()) return;
      $("redeem-code").value = "";
      UI.enter($("wallet-available"), { scale: 0.9, y: 0, duration: 420 });
      status(`兑换成功，已获得 ${data.points} 小鱼干。`);
    });
  };
  $("password-form").onsubmit = (e) => {
    e.preventDefault();
    const body = {
      currentPassword: $("password-current").value,
      newPassword: $("password-new").value,
    };
    submit(e.target, async isCurrent => {
      assign(await request("/api/auth/password", { method: "POST", body }));
      if (!isCurrent()) return;
      e.target.reset();
      selectAuth("login");
      status("密码已修改，请重新登录。");
    });
  };
  $("account-logout").onclick = async () => {
    if ($("account-logout").disabled) return;
    UI.busy($("account-logout"), true, "正在退出…");
    try {
      assign(await request("/api/auth/logout", { method: "POST" }));
      motion.close({ immediate: true });
      $("ledger-list").replaceChildren();
      $("jobs-list").replaceChildren();
    } catch (error) {
      status(error.message, true);
    } finally { UI.busy($("account-logout"), false); }
  };
  $("account-prev").onclick = () => showPane(pane, Math.max(0, offset - 50));
  $("account-next").onclick = () => showPane(pane, offset + 50);
  window.addEventListener("focus", () => {
    if (user) refreshBalance().catch(() => {});
  });
  window.addEventListener("pagehide", () => {
    motion.close({ immediate: true, restoreFocus: false });
  });
  window.Account = {
    get user() {
      return user;
    },
    get config() {
      return config;
    },
    ready,
    request,
    refresh,
    refreshBalance,
    assign,
    open,
    requireLogin() {
      if (user) return true;
      open();
      return false;
    },
  };
})();
