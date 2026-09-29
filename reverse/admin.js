"use strict";
(() => {
  if (window.location.hostname === "admin-save-pic.naix.top")
    document.querySelectorAll("[data-public-home]").forEach((link) => {
      link.href = "https://save-pic.naix.top/";
    });
  const $ = (id) => document.getElementById(id),
    A = window.Account;
  const motion = UI.dialog($("admin-dialog"));
  let dialogRevision = 0, cardsRevision = 0, pageRevision = 0, pageActive = true,
    currentRole = A.user?.role;
  let section = "users",
    offset = 0,
    revision = 0,
    currentUser = null,
    confirmAction = null,
    csvURL = null;
  const text = (tag, value, className) => {
    const e = document.createElement(tag);
    e.textContent = value ?? "";
    if (className) e.className = className;
    return e;
  };
  const escapeHtml = (str) =>
    String(str ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  const date = (v) =>
    v ? new Date(v).toLocaleString("zh-CN", { hour12: false }) : "—";
  const kindLabel = (kind) => {
    const map = {
      welcome: "注册赠送",
      redeem: "卡密兑换",
      adjust: "管理员调账",
      coin_purchase: "微信充值",
      charge: "修复扣除",
      job_settle: "任务结算",
      refund: "失败退款",
      job_refund: "失败退款",
      refund_recovery: "退款抵扣",
    };
    return map[kind] || kind;
  };
  const status = (message = "", error = false) => {
    $("admin-status").textContent = message;
    $("admin-status").classList.toggle("error", error);
  };
  const pendingWrites = new Map();
  const write = async (path, body) => {
    const owner = A.user?.id,
      key = JSON.stringify([owner, path, body]),
      id = pendingWrites.get(key) || crypto.randomUUID();
    pendingWrites.set(key, id);
    try {
      const data = await A.request(path, {
        method: "POST",
        body,
        headers: { "X-Rescue-Request-Id": id },
      });
      pendingWrites.delete(key);
      if (owner !== A.user?.id) throw new Error("账户已变更，请重新查看。");
      return data;
    } catch (error) {
      if (error.code && error.status < 500) pendingWrites.delete(key);
      throw error;
    }
  };
  function clearSecrets() {
    $("card-codes").value = "";
    $("card-secrets").hidden = true;
    if (csvURL) URL.revokeObjectURL(csvURL);
    csvURL = null;
    $("card-csv").removeAttribute("href");
  }
  function gate() {
    if (!pageActive) {
      $("admin-content").hidden = true;
      if ($("admin-gate-screen")) $("admin-gate-screen").hidden = false;
      return false;
    }
    const allowed = A.user?.role === "admin";
    $("admin-content").hidden = !allowed;
    if ($("admin-gate-screen")) $("admin-gate-screen").hidden = allowed;
    if (allowed && $("sidebar-admin-user")) {
      $("sidebar-admin-user").textContent = A.user?.username || "管理员";
    }
    $("admin-gate").textContent = allowed
      ? "账户与卡密管理，所有小鱼干变动均保留流水。"
      : A.user
        ? "当前账号没有管理员权限。"
        : "请先登录管理员账号。首次使用请在本机终端运行 npm run admin:create。";
    if (!allowed) {
      revision++;
      clearSecrets();
      $("admin-table").replaceChildren();
      motion.close({ immediate: true });
      $("reset-value").value = "";
    }
    return allowed;
  }
  function action(label, handler) {
    const b = text("button", label, "text-button");
    b.type = "button";
    b.onclick = handler;
    if (/停用|作废|重置/.test(label)) b.classList.add("danger");
    return b;
  }
  function openDialog(title) {
    dialogRevision++;
    cardsRevision++;
    $("admin-dialog-title").textContent = title;
    $("admin-dialog-status").textContent = "";
    $("adjust-form").hidden = true;
    $("confirm-action").hidden = true;
    $("reset-secret").hidden = true;
    $("batch-card-list").hidden = true;
    const detailView = $("user-detail-view");
    if (detailView) {
      detailView.hidden = true;
      detailView.replaceChildren();
    }
    const photoView = $("job-photo-view");
    if (photoView) {
      photoView.hidden = true;
      photoView.replaceChildren();
    }
    const dialog = $("admin-dialog");
    if (dialog) dialog.classList.remove("dialog-wide");
    $("reset-value").value = "";
    motion.open();
  }
  function confirm(title, description, fn) {
    openDialog(title);
    $("confirm-action").hidden = false;
    $("confirm-description").textContent = description;
    $("confirm-submit").classList.toggle("danger", /状态|作废|重置/.test(title));
    confirmAction = fn;
  }

  async function refreshStats() {
    if (!gate()) return;
    try {
      const stats = await A.request("/api/admin/stats");
      if (!stats) return;
      if ($("metric-users-total")) {
        $("metric-users-total").textContent = (stats.users?.total || 0).toLocaleString();
        $("metric-users-sub").textContent = `今日 +${stats.users?.todayNew || 0} · 活跃 ${stats.users?.active || 0} · 停用 ${stats.users?.disabled || 0}`;
        $("metric-fish-available").textContent = (stats.wallets?.totalAvailable || 0).toLocaleString();
        $("metric-fish-held").textContent = `进行中冻结 ${(stats.wallets?.totalHeld || 0).toLocaleString()} 小鱼干`;
        $("metric-fish-consumed").textContent = (stats.ledger?.totalConsumed || 0).toLocaleString();
        $("metric-jobs-sub").textContent = `成功修复 ${stats.jobs?.succeeded || 0} · 失败退款 ${stats.jobs?.failed || 0}`;
        $("metric-recharge-total").textContent = `¥ ${((stats.recharge?.totalFen || 0) / 100).toFixed(2)}`;
        $("metric-recharge-sub").textContent = `今日 ¥ ${((stats.recharge?.todayFen || 0) / 100).toFixed(2)} · 结算 ${stats.recharge?.paidOrders || 0} 笔`;
      }
    } catch (_) {}
  }

  function openAdjust(user) {
    currentUser = user;
    openDialog(`调整小鱼干 · ${user.username}`);
    $("adjust-form").reset();
    $("adjust-form").hidden = false;
    const curr = user.available || 0;
    if ($("adjust-curr-num")) $("adjust-curr-num").textContent = curr;
    if ($("adjust-next-num")) {
      $("adjust-next-num").textContent = curr;
      $("adjust-next-num").classList.remove("danger");
    }
    $("adjust-delta").focus();
  }

  function showUserLedger(user) {
    section = "ledger";
    if ($("ledger-user-query")) $("ledger-user-query").value = user.id;
    const tab = document.querySelector('[data-admin-pane="ledger"]');
    if (tab) adminTabs.select(tab);
    $("admin-section-title").textContent = "小鱼干流水";
    offset = 0;
    table();
  }

  async function showUserDetail(user) {
    openDialog(`用户画像与治理 · ${user.username}`);
    const detailView = $("user-detail-view");
    if (!detailView) return;
    detailView.hidden = false;
    detailView.replaceChildren();
    UI.loading(detailView);
    try {
      const detail = await A.request(`/api/admin/users/${user.id}/detail`);
      if (!$("admin-dialog").open) return;
      detailView.removeAttribute("aria-busy");
      detailView.replaceChildren();

      const container = text("div", "", "user-detail-container");
      container.innerHTML = `
        <div class="user-profile-card">
          <div class="user-profile-meta">
            <h3 class="user-profile-name">${escapeHtml(user.username)} ${user.role === "admin" ? '<span class="status-badge pending">管理员</span>' : ""} ${user.disabled ? '<span class="status-badge muted">已停用</span>' : '<span class="status-badge success">正常</span>'}</h3>
            <p class="user-id-line">用户ID: <code id="copy-user-id" title="点击复制">${user.id}</code></p>
            ${detail.wechat ? `<p class="user-wechat-line">微信 OpenID: <code>${escapeHtml(detail.wechat.openId)}</code></p>` : '<p class="subtle">未关联微信小程序身份</p>'}
          </div>
          <div class="user-profile-actions">
            <button class="button primary btn-sm" id="detail-btn-adjust" type="button">调整小鱼干</button>
          </div>
        </div>
        <div class="detail-stat-row">
          <div class="detail-stat-pill">
            <span class="subtle">可用小鱼干</span>
            <strong class="gold-num">${detail.user.available}</strong>
          </div>
          <div class="detail-stat-pill">
            <span class="subtle">冻结中</span>
            <strong>${detail.user.held}</strong>
          </div>
          <div class="detail-stat-pill">
            <span class="subtle">累计充值实收</span>
            <strong>¥${((detail.recharge?.totalFen || 0) / 100).toFixed(2)}</strong>
            <small class="subtle">已结算 ${detail.recharge?.ordersCount || 0} 笔</small>
          </div>
          <div class="detail-stat-pill">
            <span class="subtle">退款待抵扣负债</span>
            <strong class="${detail.debt > 0 ? "danger-text" : ""}">${detail.debt} 小鱼干</strong>
          </div>
        </div>
        <div class="detail-section">
          <div class="section-title-row">
            <h4>账本流水分类汇总</h4>
          </div>
          <div class="kind-summary-grid">
            ${(detail.ledgerSummary || []).map((s) => `
              <div class="kind-summary-item">
                <span class="kind-tag">${escapeHtml(kindLabel(s.kind))}</span>
                <span class="kind-stats">收入 <strong>+${s.totalIn}</strong> / 扣除 <strong>-${s.totalOut}</strong> (${s.count}笔)</span>
              </div>
            `).join("")}
          </div>
        </div>
        <div class="detail-section">
          <div class="section-title-row">
            <h4>最近变动流水 (前 20 条)</h4>
            <button class="text-button" id="detail-btn-full-ledger" type="button">查看该用户全部流水 →</button>
          </div>
          <div class="detail-records-list">
            ${(detail.recentLedger || []).map((l) => `
              <div class="record">
                <div>
                  <strong>${escapeHtml(kindLabel(l.kind))} · <span class="${l.available_delta >= 0 ? "gold-num" : "danger-text"}">${l.available_delta >= 0 ? "+" + l.available_delta : l.available_delta}</span></strong>
                  <p class="subtle">${escapeHtml(l.reason)}</p>
                </div>
                <div class="record-meta">
                  <span>变动后可用: ${l.available_after}</span>
                  <small>${date(l.created_at)}</small>
                </div>
              </div>
            `).join("")}
          </div>
        </div>
      `;
      detailView.append(container);

      const copyId = container.querySelector("#copy-user-id");
      if (copyId) {
        copyId.onclick = async () => {
          try {
            await navigator.clipboard.writeText(user.id);
            status("用户 ID 已复制到剪贴板。");
          } catch (_) {}
        };
      }

      const adjustBtn = container.querySelector("#detail-btn-adjust");
      if (adjustBtn) adjustBtn.onclick = () => openAdjust(user);

      const fullLedgerBtn = container.querySelector("#detail-btn-full-ledger");
      if (fullLedgerBtn) fullLedgerBtn.onclick = () => {
        motion.close();
        showUserLedger(user);
      };
    } catch (error) {
      UI.loadError(detailView, error.message, () => showUserDetail(user));
    }
  }

  let activeLightboxJob = null;
  let activeLightboxKind = "original";
  let isActualSize = false;
  let isLightboxHolding = false;
  let lightboxInitialized = false;

  function initLightboxEvents() {
    if (lightboxInitialized) return;
    const lbDialog = $("photo-lightbox-dialog");
    if (!lbDialog) return;
    lightboxInitialized = true;

    const closeBtn = $("lightbox-btn-close");
    if (closeBtn) closeBtn.onclick = closePhotoLightbox;

    const btnOrig = $("lightbox-btn-orig");
    if (btnOrig) {
      btnOrig.onclick = (e) => {
        e.stopPropagation();
        activeLightboxKind = "original";
        updateLightboxView();
      };
    }

    const btnRes = $("lightbox-btn-res");
    if (btnRes) {
      btnRes.onclick = (e) => {
        e.stopPropagation();
        activeLightboxKind = "result";
        updateLightboxView();
      };
    }

    const btnZoom = $("lightbox-btn-zoom");
    if (btnZoom) {
      btnZoom.onclick = (e) => {
        e.stopPropagation();
        isActualSize = !isActualSize;
        updateLightboxView();
      };
    }

    const btnHold = $("lightbox-btn-hold");
    if (btnHold) {
      const startHold = (e) => {
        e.preventDefault();
        e.stopPropagation();
        if (isLightboxHolding || !activeLightboxJob || activeLightboxJob.status !== "succeeded") return;
        isLightboxHolding = true;
        btnHold.classList.add("active");
        btnHold.textContent = "👁️ 正在显示原图...";
        const img = $("lightbox-img");
        if (img) img.src = `/api/admin/jobs/${activeLightboxJob.id}/photo/original`;
        const badge = $("lightbox-badge");
        if (badge) {
          badge.textContent = "原图（按住对比中）";
          badge.className = "lightbox-badge original";
        }
      };
      const endHold = (e) => {
        if (e) { e.preventDefault(); e.stopPropagation(); }
        if (!isLightboxHolding) return;
        isLightboxHolding = false;
        btnHold.classList.remove("active");
        btnHold.textContent = "🔘 按住看原图";
        updateLightboxView();
      };
      btnHold.addEventListener("mousedown", startHold);
      window.addEventListener("mouseup", endHold);
      btnHold.addEventListener("touchstart", startHold, { passive: false });
      window.addEventListener("touchend", endHold);
    }

    const stage = $("lightbox-stage");
    if (stage) {
      stage.addEventListener("click", (e) => {
        if (e.target === stage || e.target === $("lightbox-image-box")) {
          closePhotoLightbox();
        }
      });
    }

    const img = $("lightbox-img");
    if (img) {
      img.addEventListener("click", (e) => {
        e.stopPropagation();
        isActualSize = !isActualSize;
        updateLightboxView();
      });
    }

    lbDialog.addEventListener("cancel", (e) => {
      e.preventDefault();
      closePhotoLightbox();
    });

    window.addEventListener("keydown", (e) => {
      if (!lbDialog.open || !activeLightboxJob) return;
      if (e.key === "Escape") {
        closePhotoLightbox();
      } else if (e.key === "ArrowLeft") {
        activeLightboxKind = "original";
        updateLightboxView();
      } else if (e.key === "ArrowRight" && activeLightboxJob.status === "succeeded") {
        activeLightboxKind = "result";
        updateLightboxView();
      }
    });
  }

  function openPhotoLightbox(job, kind = "original") {
    initLightboxEvents();
    activeLightboxJob = job;
    activeLightboxKind = kind;
    isActualSize = false;
    isLightboxHolding = false;

    const lbDialog = $("photo-lightbox-dialog");
    if (!lbDialog) return;

    updateLightboxView();
    if (!lbDialog.open) {
      if (typeof lbDialog.showModal === "function") {
        lbDialog.showModal();
      } else {
        lbDialog.setAttribute("open", "");
      }
    }
  }

  function closePhotoLightbox() {
    const lbDialog = $("photo-lightbox-dialog");
    if (lbDialog && lbDialog.open) {
      if (typeof lbDialog.close === "function") {
        lbDialog.close();
      } else {
        lbDialog.removeAttribute("open");
      }
    }
    activeLightboxJob = null;
    isLightboxHolding = false;
  }

  function updateLightboxView() {
    const job = activeLightboxJob;
    if (!job) return;

    const img = $("lightbox-img");
    const badge = $("lightbox-badge");
    const info = $("lightbox-info");
    const sw = $("lightbox-switch");
    const btnOrig = $("lightbox-btn-orig");
    const btnRes = $("lightbox-btn-res");
    const btnHold = $("lightbox-btn-hold");
    const btnZoom = $("lightbox-btn-zoom");
    const btnDown = $("lightbox-btn-download");
    const imgBox = $("lightbox-image-box");

    const isSucceeded = job.status === "succeeded";
    const kind = activeLightboxKind;
    const url = `/api/admin/jobs/${job.id}/photo/${kind}`;

    if (img) {
      img.src = url;
      img.alt = kind === "original" ? "修复前原图" : "修复后效果";
    }

    if (badge) {
      badge.textContent = kind === "original" ? "原图（修复前）" : "修复后（拯救效果）";
      badge.className = `lightbox-badge ${kind}`;
    }

    if (info) {
      const parts = [escapeHtml(job.username)];
      if (job.quality) parts.push(job.quality === "light" ? "轻量修复" : "精细修复");
      if (job.cost) parts.push(`${job.cost} 小鱼干`);
      if (job.width && job.height) parts.push(`${job.width} × ${job.height}px`);
      info.innerHTML = parts.join(" · ");
    }

    if (sw) sw.hidden = !isSucceeded;
    if (btnHold) btnHold.hidden = !isSucceeded;

    if (btnOrig) btnOrig.classList.toggle("active", kind === "original");
    if (btnRes) btnRes.classList.toggle("active", kind === "result");

    if (btnDown) {
      btnDown.href = `${url}?download=1`;
      btnDown.download = `job-${job.id.slice(0, 8)}-${kind}.jpg`;
    }

    if (imgBox) {
      imgBox.classList.toggle("actual-size", isActualSize);
    }
    if (btnZoom) {
      btnZoom.textContent = isActualSize ? "🔍 适应窗口" : "🔍 100% 原始尺寸";
    }
  }

  function showJobPhotos(job) {
    openDialog(`修复记录照片对比 · ${job.username}`);
    const dialog = $("admin-dialog");
    if (dialog) dialog.classList.add("dialog-wide");

    const view = $("job-photo-view");
    if (!view) return;
    view.hidden = false;
    view.replaceChildren();

    const isSucceeded = job.status === "succeeded";
    const isFailed = job.status === "failed";
    const isRunning = job.status === "running";
    const statusLabel = {
      running: "进行中",
      succeeded: job.expires_at && job.expires_at <= Date.now() ? "已完成 · 照片过期" : "已完成",
      failed: "失败已退款",
    }[job.status] || job.status;
    const statusKind = isRunning ? "pending" : isFailed ? "error" : "success";

    const container = text("div", "", "job-photo-container");

    const originalUrl = `/api/admin/jobs/${job.id}/photo/original`;
    const resultUrl = `/api/admin/jobs/${job.id}/photo/result`;

    container.innerHTML = `
      <div class="job-meta-card">
        <div class="job-meta-header">
          <div class="job-meta-title-row">
            <div class="job-meta-badges">
              <strong>${escapeHtml(job.username)}</strong>
              <span class="status-badge ${statusKind}">${escapeHtml(statusLabel)}</span>
              <span class="quality-badge">${job.quality === "light" ? "轻量修复" : "精细修复"} · ${job.cost} 小鱼干</span>
            </div>
            ${isSucceeded ? `
              <button type="button" class="hold-to-compare-trigger" id="hold-compare-btn" title="长按此处显示原图，松开恢复效果图">
                🔘 按住查看原图
              </button>
            ` : ""}
          </div>
          <div class="job-meta-details-grid">
            <div class="job-meta-item"><span>任务编号：</span><code id="copy-job-id" title="点击复制">${job.id}</code></div>
            <div class="job-meta-item"><span>原始文件：</span><strong>${escapeHtml(job.filename || "—")}</strong></div>
            ${job.width && job.height ? `<div class="job-meta-item"><span>画面尺寸：</span><strong>${job.width} × ${job.height} px</strong></div>` : ""}
            <div class="job-meta-item"><span>提交时间：</span><span>${date(job.created_at)}</span></div>
            ${job.finished_at ? `<div class="job-meta-item"><span>完成时间：</span><span>${date(job.finished_at)}</span></div>` : ""}
            ${job.error_code || job.error_message ? `<div class="job-meta-item danger-text"><span>错误原因：</span><strong>${escapeHtml(job.error_message || job.error_code)}</strong></div>` : ""}
          </div>
        </div>
      </div>

      <div class="photo-compare-toolbar">
        <div class="compare-mode-tabs" id="compare-mode-tabs">
          <button type="button" class="compare-tab-btn active" data-mode="split">并排对比</button>
          <button type="button" class="compare-tab-btn" data-mode="result">仅看修复图</button>
          <button type="button" class="compare-tab-btn" data-mode="original">仅看原图</button>
        </div>
        <span class="subtle">提示：点击照片可居中全屏放大查看真实像素，支持在新标签页打开或下载</span>
      </div>

      <div class="photo-compare-grid" id="photo-compare-grid">
        <!-- 原图 -->
        <div class="photo-compare-card" id="card-original">
          <div class="photo-card-head">
            <span class="photo-tag-title"><span class="photo-dot original"></span>原图（修复前）</span>
            <div class="photo-card-actions">
              <button type="button" class="text-button btn-zoom-orig">🔍 居中放大</button>
              <a href="${originalUrl}" target="_blank" rel="noopener" class="text-button">新窗口</a>
              <a href="${originalUrl}?download=1" class="text-button" download>下载原图</a>
            </div>
          </div>
          <div class="photo-display-frame" id="frame-original" title="点击照片放大到页面中央">
            <img src="${originalUrl}" alt="修复前原图" id="img-original" loading="lazy" />
            <div class="photo-zoom-hint"><span>🔍 点击居中放大</span></div>
          </div>
        </div>

        <!-- 效果图 -->
        <div class="photo-compare-card" id="card-result">
          <div class="photo-card-head">
            <span class="photo-tag-title"><span class="photo-dot result"></span>修复后（拯救效果）</span>
            <div class="photo-card-actions">
              ${isSucceeded ? `
                <button type="button" class="text-button btn-zoom-res">🔍 居中放大</button>
                <a href="${resultUrl}" target="_blank" rel="noopener" class="text-button">新窗口</a>
                <a href="${resultUrl}?download=1" class="text-button" download>下载效果图</a>
              ` : ""}
            </div>
          </div>
          <div class="photo-display-frame" id="frame-result" title="${isSucceeded ? '点击照片放大到页面中央' : ''}">
            ${isSucceeded ? `
              <img src="${resultUrl}" alt="修复后效果" id="img-result" loading="lazy" />
              <div class="photo-zoom-hint"><span>🔍 点击居中放大</span></div>
            ` : `
              <div class="photo-empty-box">
                <span class="icon">${isFailed ? "⚠️" : "⏳"}</span>
                <strong>${isFailed ? "修复未成功" : "处理中"}</strong>
                <p class="subtle">${escapeHtml(job.error_message || (isFailed ? "小鱼干已原路全额退还" : "正在生成中，请稍后刷新"))}</p>
              </div>
            `}
          </div>
        </div>
      </div>
    `;

    view.append(container);

    const copyJobId = container.querySelector("#copy-job-id");
    if (copyJobId) {
      copyJobId.onclick = async () => {
        try {
          await navigator.clipboard.writeText(job.id);
          status("任务 ID 已复制到剪贴板。");
        } catch (_) {}
      };
    }

    const frameOrig = container.querySelector("#frame-original");
    if (frameOrig) {
      frameOrig.addEventListener("click", () => openPhotoLightbox(job, "original"));
    }
    const btnZoomOrig = container.querySelector(".btn-zoom-orig");
    if (btnZoomOrig) {
      btnZoomOrig.addEventListener("click", (e) => {
        e.stopPropagation();
        openPhotoLightbox(job, "original");
      });
    }

    const frameRes = container.querySelector("#frame-result");
    if (frameRes && isSucceeded) {
      frameRes.addEventListener("click", () => openPhotoLightbox(job, "result"));
    }
    const btnZoomRes = container.querySelector(".btn-zoom-res");
    if (btnZoomRes) {
      btnZoomRes.addEventListener("click", (e) => {
        e.stopPropagation();
        openPhotoLightbox(job, "result");
      });
    }

    const imgOrig = container.querySelector("#img-original");
    if (imgOrig) {
      imgOrig.onerror = () => {
        const frame = container.querySelector("#frame-original");
        if (frame) {
          frame.innerHTML = `
            <div class="photo-empty-box">
              <span class="icon">📷</span>
              <p>原图文件不可用或已过期清理</p>
            </div>
          `;
        }
      };
    }

    const imgResult = container.querySelector("#img-result");
    if (imgResult) {
      imgResult.onerror = () => {
        const frame = container.querySelector("#frame-result");
        if (frame) {
          frame.innerHTML = `
            <div class="photo-empty-box">
              <span class="icon">🖼️</span>
              <p>修复图文件不可用或已过期清理</p>
            </div>
          `;
        }
      };
    }

    const holdBtn = container.querySelector("#hold-compare-btn");
    if (holdBtn && imgResult) {
      let isHolding = false;
      const startHold = (e) => {
        e.preventDefault();
        if (isHolding) return;
        isHolding = true;
        holdBtn.classList.add("active");
        holdBtn.textContent = "👁️ 正在显示原图...";
        imgResult.dataset.oldSrc = imgResult.src;
        imgResult.src = originalUrl;
      };
      const endHold = (e) => {
        if (e) e.preventDefault();
        if (!isHolding) return;
        isHolding = false;
        holdBtn.classList.remove("active");
        holdBtn.textContent = "🔘 按住查看原图";
        if (imgResult.dataset.oldSrc) {
          imgResult.src = imgResult.dataset.oldSrc;
        }
      };
      holdBtn.addEventListener("mousedown", startHold);
      window.addEventListener("mouseup", endHold, { once: true });
      holdBtn.addEventListener("touchstart", startHold, { passive: false });
      window.addEventListener("touchend", endHold, { once: true });
    }

    const modeTabs = container.querySelectorAll(".compare-tab-btn");
    const grid = container.querySelector("#photo-compare-grid");
    const cardOrig = container.querySelector("#card-original");
    const cardRes = container.querySelector("#card-result");

    modeTabs.forEach(btn => {
      btn.addEventListener("click", () => {
        modeTabs.forEach(b => b.classList.remove("active"));
        btn.classList.add("active");
        const mode = btn.dataset.mode;
        if (mode === "split") {
          grid.style.display = "grid";
          grid.style.gridTemplateColumns = window.innerWidth > 768 ? "1fr 1fr" : "1fr";
          if (cardOrig) cardOrig.style.display = "flex";
          if (cardRes) cardRes.style.display = "flex";
        } else if (mode === "result") {
          grid.style.display = "block";
          if (cardOrig) cardOrig.style.display = "none";
          if (cardRes) cardRes.style.display = "flex";
        } else if (mode === "original") {
          grid.style.display = "block";
          if (cardOrig) cardOrig.style.display = "flex";
          if (cardRes) cardRes.style.display = "none";
        }
      });
    });
  }

  async function table() {
    if (!gate()) return;
    const own = ++revision,
      owner = A.user.id;
    status();
    refreshStats();
    $("admin-search").hidden = section !== "users";
    if ($("ledger-filter")) $("ledger-filter").hidden = section !== "ledger";
    if ($("orders-filter")) $("orders-filter").hidden = section !== "orders";
    $("batch-form").hidden = section !== "batches";
    $("config-form").hidden = section !== "config";
    $("admin-pagination").hidden = section === "config";
    $("admin-table").hidden = section === "config";
    $("admin-prev").disabled = true;
    $("admin-next").disabled = true;
    UI.loading($("admin-table"));
    const configControls = [...$("config-form").querySelectorAll("input,button")];
    if (section === "config") {
      configControls.forEach(e => e.disabled = true);
      $("config-form").setAttribute("aria-busy", "true");
      status("正在读取配置…");
    }
    try {
      if (section === "config") {
        const config = await A.request("/api/admin/config");
        if (own !== revision) return;
        $("price-light").value = config.prices.light;
        $("price-fine").value = config.prices.fine;
        $("welcome-points").value = config.welcome;
        configControls.forEach(e => e.disabled = false);
        $("config-form").removeAttribute("aria-busy");
        status();
        return;
      }
      let query = "";
      if (section === "users") {
        const q = $("user-query")?.value || "";
        const sFilter = $("user-status-filter")?.value || "all";
        const sortFilter = $("user-sort-filter")?.value || "created_desc";
        query = `&q=${encodeURIComponent(q)}&status=${encodeURIComponent(sFilter)}&sort=${encodeURIComponent(sortFilter)}`;
      } else if (section === "ledger") {
        const uQuery = $("ledger-user-query")?.value || "";
        const kFilter = $("ledger-kind-filter")?.value || "all";
        query = `&userId=${encodeURIComponent(uQuery)}&kind=${encodeURIComponent(kFilter)}`;
      } else if (section === "orders") {
        const oQuery = $("orders-query")?.value || "";
        const osFilter = $("orders-status-filter")?.value || "all";
        query = `&q=${encodeURIComponent(oQuery)}&status=${encodeURIComponent(osFilter)}`;
      }

      const data = await A.request(
        `/api/admin/${section}?offset=${offset}${query}`,
      );
      if (own !== revision || owner !== A.user?.id) return;
      const headers = {
        users: ["用户名", "可用 / 冻结", "状态", "充值实收 / 消耗", "注册时间", "操作"],
        batches: ["批次", "小鱼干 × 数量", "已兑 / 作废", "有效期", "操作"],
        jobs: ["用户", "档位 / 小鱼干", "状态", "创建时间", "错误", "操作"],
        ledger: ["流水ID", "用户", "类型 / 凭据", "可用变化", "冻结变化", "变动后余额", "说明", "时间"],
        orders: ["商户单号", "用户", "商品", "实付金额", "小鱼干", "状态", "微信单号", "时间"],
        audit: ["管理员", "操作", "目标", "记录", "时间"],
      }[section];
      const table = text("table", ""),
        thead = text("thead", ""),
        head = text("tr", "");
      headers.forEach((h) => { const cell = text("th", h); cell.scope = "col"; head.append(cell); });
      thead.append(head);
      table.append(thead);
      const tbody = text("tbody", "");
      for (const item of data.items) {
        const row = text("tr", "");
        let values = [];
        if (section === "users")
          values = [
            item.username + (item.role === "admin" ? " · 管理员" : ""),
            `${item.available} / ${item.held}`,
            item.disabled ? "已停用" : "正常",
            `¥${((item.rechargeFen || 0) / 100).toFixed(2)} / ${item.creditConsumed || 0}`,
            date(item.created_at),
          ];
        if (section === "batches")
          values = [
            item.id.slice(0, 8),
            `${item.points} × ${item.quantity}`,
            `${item.redeemed} / ${item.voided}`,
            item.expires_at ? date(item.expires_at) : "不过期",
          ];
        if (section === "jobs")
          values = [
            item.username,
            `${item.quality === "light" ? "轻量" : "精细"} / ${item.cost}`,
            {
              running: "进行中",
              succeeded:
                item.expires_at <= Date.now() ? "已完成 · 照片过期" : "已完成",
              failed: "失败已退款",
            }[item.status],
            date(item.created_at),
            item.error_code || "—",
          ];
        if (section === "ledger")
          values = [
            item.id,
            item.username,
            `${kindLabel(item.kind)}${item.reference ? " · " + item.reference.slice(0, 16) : ""}`,
            item.available_delta >= 0 ? `+${item.available_delta}` : `${item.available_delta}`,
            item.held_delta,
            `${item.available_after} / 冻结 ${item.held_after}`,
            item.reason,
            date(item.created_at),
          ];
        if (section === "orders")
          values = [
            item.out_trade_no,
            item.username,
            `${item.product_title} × ${item.quantity}`,
            `¥${(item.amount_fen / 100).toFixed(2)}`,
            `${item.fish_amount} 小鱼干`,
            item.status === "delivered" ? "已发货" : "待支付",
            item.wx_order_id || "—",
            date(item.created_at),
          ];
        if (section === "audit")
          values = [
            item.username,
            item.action,
            item.target,
            item.details,
            date(item.created_at),
          ];
        values.forEach((v, index) => {
          const cell = text("td", v);
          if (index === 2 && ["users", "jobs"].includes(section)) {
            const kind = section === "users" ? (item.disabled ? "muted" : "success") :
              item.status === "running" ? "pending" : item.status === "failed" ? "error" : "success";
            cell.replaceChildren(text("span", v, `status-badge ${kind}`));
          }
          if (section === "orders" && index === 5) {
            const kind = item.status === "delivered" ? "success" : "pending";
            cell.replaceChildren(text("span", v, `status-badge ${kind}`));
          }
          if (section === "ledger" && index === 3) {
            if (item.available_delta > 0) cell.classList.add("gold-num");
            else if (item.available_delta < 0) cell.classList.add("danger-text");
          }
          row.append(cell);
        });
        if (section === "users") {
          const cell = text("td", ""),
            buttons = text("div", "", "table-actions");
          buttons.append(
            action("详情", () => showUserDetail(item)),
            action("流水", () => showUserLedger(item)),
            action("调整小鱼干", () => openAdjust(item)),
          );
          if (item.role !== "admin")
            buttons.append(
              action(item.disabled ? "启用" : "停用", () =>
                confirm(
                  "更改账户状态",
                  `将${item.disabled ? "启用" : "停用"} ${item.username}；停用会注销登录，已接受的修复仍将继续。`,
                  async () => {
                    const ownDialog = dialogRevision;
                    await write(`/api/admin/users/${item.id}/status`, {
                      disabled: !item.disabled,
                    });
                    if (ownDialog === dialogRevision) motion.close();
                    table();
                  },
                ),
              ),
            );
          buttons.append(
            action("重置密码", () =>
              confirm(
                "生成密码重置凭据",
                `为 ${item.username} 生成新的单次凭据。用户完成重置后，旧会话会全部退出。`,
                async () => {
                  const ownDialog = dialogRevision;
                  const data = await write(
                    `/api/admin/users/${item.id}/reset`,
                    {},
                  );
                  if (ownDialog !== dialogRevision) return;
                  $("confirm-action").hidden = true;
                  $("reset-secret").hidden = false;
                  $("reset-value").value = data.token;
                },
              ),
            ),
          );
          cell.append(buttons);
          row.append(cell);
        }
        if (section === "batches") {
          const cell = text("td", ""),
            buttons = text("div", "", "table-actions");
          buttons.append(
            action("查看", () => showCards(item)),
            action("作废未用卡密", () =>
              confirm(
                "作废未使用卡密",
                `批次 ${item.id.slice(0, 8)} 的未使用卡密将失效，已兑换小鱼干不受影响。`,
                async () => {
                  const ownDialog = dialogRevision;
                  const data = await write(
                    `/api/admin/batches/${item.id}/void`,
                    {},
                  );
                  if (ownDialog === dialogRevision) motion.close();
                  await table();
                  status(`已作废 ${data.count} 张未使用卡密。`);
                },
              ),
            ),
          );
          cell.append(buttons);
          row.append(cell);
        }
        if (section === "jobs") {
          row.classList.add("clickable-job-row");
          row.title = "点击查看原图与修复效果对比";
          row.addEventListener("click", (e) => {
            if (e.target.closest("button, a, input")) return;
            showJobPhotos(item);
          });
          const cell = text("td", "", "job-action-col"),
            buttons = text("div", "", "table-actions");
          buttons.append(
            action("查看对比", () => showJobPhotos(item)),
          );
          cell.append(buttons);
          row.append(cell);
        }
        row.querySelectorAll("button").forEach(button => {
          button.dataset.focusKey = `${section}:${item.id}:${button.textContent}`;
        });
        tbody.append(row);
      }
      table.append(tbody);
      $("admin-table").removeAttribute("aria-busy");
      $("admin-table").tabIndex = 0;
      $("admin-table").setAttribute("role", "region");
      $("admin-table").setAttribute("aria-label", "管理数据列表，可横向滚动查看全部列");
      $("admin-table").replaceChildren(
        data.items.length ? table : text("p", "暂无记录。", "empty-record"),
      );
      UI.enter($("admin-table"));
      $("admin-prev").disabled = offset === 0;
      $("admin-next").disabled = data.items.length < 50;
    } catch (error) {
      if (own !== revision || owner !== A.user?.id) return;
      if (section === "config") {
        $("config-form").removeAttribute("aria-busy");
        $("admin-table").hidden = false;
      }
      UI.loadError($("admin-table"), error.message, table);
    }
  }
  async function showCards(batch, start = 0) {
    const owner = A.user?.id;
    if (!$("admin-dialog").open)
      openDialog(`卡密批次 · ${batch.id.slice(0, 8)}`);
    const ownCards = ++cardsRevision;
    $("batch-card-list").hidden = false;
    const target = $("batch-card-list");
    UI.loading(target);
    try {
      const data = await A.request(
        `/api/admin/batches/${batch.id}/cards?offset=${start}`,
      );
      if (
        ownCards !== cardsRevision ||
        owner !== A.user?.id ||
        A.user?.role !== "admin" ||
        !$("admin-dialog").open
      )
        return;
      target.removeAttribute("aria-busy");
      target.replaceChildren();
      for (const card of data.items) {
        const row = text("div", "", "record");
        row.append(
          text("strong", `尾号 ${card.tail} · ${card.points} 小鱼干`),
          text(
            "p",
            `${{ unused: "未使用", redeemed: "已兑换", void: "已作废" }[card.status]}${card.expires_at && card.expires_at <= Date.now() && card.status === "unused" ? " · 已过期" : ""}${card.redeemedBy ? " · " + card.redeemedBy : ""}`,
          ),
          text(
            "small",
            card.redeemed_at
              ? date(card.redeemed_at)
              : card.expires_at
                ? "有效至 " + date(card.expires_at)
                : "不过期",
          ),
        );
        target.append(row);
      }
      const pagination = text("div", "", "pagination");
      if (start)
        pagination.append(action("上一页", () => showCards(batch, start - 50)));
      if (data.items.length === 50)
        pagination.append(action("下一页", () => showCards(batch, start + 50)));
      target.append(pagination);
    } catch (error) {
      if (ownCards === cardsRevision) UI.loadError(target, error.message, () => showCards(batch, start));
    }
  }
  async function formAction(form, fn) {
    if (form.getAttribute("aria-busy") === "true") return;
    const own = revision;
    UI.busy(form, true);
    try { await fn(); }
    catch (error) { if (own === revision) status(error.message, true); }
    finally { UI.busy(form, false); }
  }
  const adminTabs = UI.tabs($("admin-nav"), '[data-admin-pane]', () => $("admin-view"), b => {
    clearSecrets();
    section = b.dataset.adminPane;
    $("admin-section-title").textContent = b.textContent;
    if ($("admin-bc-current")) $("admin-bc-current").textContent = b.textContent;
    offset = 0;
    adminTabs.select(b);
    table();
  });
  if ($("admin-refresh-btn")) {
    $("admin-refresh-btn").onclick = () => {
      table();
    };
  }
  const handleLogout = async () => {
    try {
      await fetch("/api/auth/logout", { method: "POST" });
    } catch (_) {}
    location.reload();
  };
  if ($("admin-logout-trigger")) $("admin-logout-trigger").onclick = handleLogout;
  if ($("mobile-logout-trigger")) $("mobile-logout-trigger").onclick = handleLogout;
  $("admin-search").onsubmit = (e) => {
    e.preventDefault();
    offset = 0;
    table();
  };
  if ($("user-reset-search")) {
    $("user-reset-search").onclick = () => {
      $("user-query").value = "";
      if ($("user-status-filter")) $("user-status-filter").value = "all";
      if ($("user-sort-filter")) $("user-sort-filter").value = "created_desc";
      offset = 0;
      table();
    };
  }
  if ($("user-status-filter")) {
    $("user-status-filter").onchange = () => {
      offset = 0;
      table();
    };
  }
  if ($("user-sort-filter")) {
    $("user-sort-filter").onchange = () => {
      offset = 0;
      table();
    };
  }
  if ($("ledger-filter")) {
    $("ledger-filter").onsubmit = (e) => {
      e.preventDefault();
      offset = 0;
      table();
    };
  }
  if ($("ledger-kind-filter")) {
    $("ledger-kind-filter").onchange = () => {
      offset = 0;
      table();
    };
  }
  if ($("ledger-export-btn")) {
    $("ledger-export-btn").onclick = () => {
      const uQuery = $("ledger-user-query")?.value || "";
      const kFilter = $("ledger-kind-filter")?.value || "all";
      window.location.href = `/api/admin/ledger?format=csv&userId=${encodeURIComponent(uQuery)}&kind=${encodeURIComponent(kFilter)}`;
    };
  }
  if ($("orders-filter")) {
    $("orders-filter").onsubmit = (e) => {
      e.preventDefault();
      offset = 0;
      table();
    };
  }
  if ($("orders-status-filter")) {
    $("orders-status-filter").onchange = () => {
      offset = 0;
      table();
    };
  }
  $("admin-prev").onclick = () => {
    offset = Math.max(0, offset - 50);
    table();
  };
  $("admin-next").onclick = () => {
    offset += 50;
    table();
  };
  $("batch-form").onsubmit = (e) => {
    e.preventDefault();
    const body = {
      points: Number($("card-points").value),
      quantity: Number($("card-quantity").value),
      expiresAt: $("card-expiry").value
        ? new Date($("card-expiry").value).getTime()
        : null,
    };
    const own = revision;
    formAction(e.target, async () => {
      clearSecrets();
      const data = await write("/api/admin/batches", body);
      if (own !== revision || section !== "batches") return;
      $("card-codes").value = data.codes.join("\n");
      csvURL = URL.createObjectURL(
        new Blob(
          [
            "\ufeff卡密,小鱼干,有效期\r\n" +
              data.codes
                .map(
                  (code) =>
                    `${code},${data.points},${data.expiresAt ? new Date(data.expiresAt).toISOString() : "不过期"}`,
                )
                .join("\r\n"),
          ],
          { type: "text/csv;charset=utf-8" },
        ),
      );
      $("card-csv").href = csvURL;
      $("card-csv").download = `卡密-${data.id.slice(0, 8)}.csv`;
      $("card-secrets").hidden = false;
      await table();
      status("卡密已生成，请下载保存明文。");
    });
  };
  $("hide-codes").onclick = clearSecrets;
  $("config-form").onsubmit = (e) => {
    e.preventDefault();
    formAction(e.target, async () => {
      await write("/api/admin/config", {
        light: Number($("price-light").value),
        fine: Number($("price-fine").value),
        welcome: Number($("welcome-points").value),
      });
      await A.refreshBalance();
      status("小鱼干配置已更新。");
    });
  };
  $("adjust-delta").oninput = () => {
    const delta = Number($("adjust-delta").value) || 0;
    const curr = currentUser?.available || 0;
    const next = curr + delta;
    if ($("adjust-curr-num")) $("adjust-curr-num").textContent = curr;
    if ($("adjust-next-num")) {
      $("adjust-next-num").textContent = next;
      $("adjust-next-num").classList.toggle("danger", next < 0);
    }
  };
  if ($("quick-reasons")) {
    $("quick-reasons").onclick = (e) => {
      const btn = e.target.closest("button[data-reason]");
      if (!btn) return;
      $("adjust-reason").value = btn.dataset.reason;
    };
  }
  $("adjust-form").onsubmit = async (e) => {
    e.preventDefault();
    const b = e.target.querySelector("button"), ownDialog = dialogRevision;
    if (b.disabled) return;
    UI.busy(b, true);
    try {
      await write(`/api/admin/users/${currentUser.id}/credits`, {
        delta: Number($("adjust-delta").value),
        reason: $("adjust-reason").value,
      });
      if (ownDialog === dialogRevision) motion.close();
      await table();
      await A.refreshBalance();
      status("小鱼干已调整，并已记录原因。");
    } catch (error) {
      if (ownDialog === dialogRevision) $("admin-dialog-status").textContent = error.message;
    } finally {
      UI.busy(b, false);
    }
  };
  $("confirm-submit").onclick = async () => {
    const b = $("confirm-submit"), ownDialog = dialogRevision;
    if (b.disabled || !confirmAction) return;
    UI.busy(b, true);
    try {
      await confirmAction?.();
    } catch (error) {
      if (ownDialog === dialogRevision) $("admin-dialog-status").textContent = error.message;
    } finally {
      UI.busy(b, false);
    }
  };
  $("admin-dialog-close").onclick = () => motion.close();
  $("admin-dialog").addEventListener("uiclose", () => {
    dialogRevision++;
    cardsRevision++;
    closePhotoLightbox();
    $("reset-value").value = "";
    confirmAction = null;
  });
  window.addEventListener("accountchange", (e) => {
    const roleChanged = currentRole !== e.detail.user?.role;
    currentRole = e.detail.user?.role;
    if (e.detail.previousId !== e.detail.user?.id || roleChanged) {
      revision++;
      clearSecrets();
      pendingWrites.clear();
      closePhotoLightbox();
      motion.close({ immediate: true });
      $("reset-value").value = "";
      gate();
      if (A.user?.role === "admin") table();
    }
  });
  window.addEventListener("pagehide", () => {
    pageActive = false;
    pageRevision++;
    revision++;
    dialogRevision++;
    cardsRevision++;
    clearSecrets();
    closePhotoLightbox();
    motion.close({ immediate: true, restoreFocus: false });
    $("reset-value").value = "";
    $("admin-content").hidden = true;
    if ($("admin-gate-screen")) $("admin-gate-screen").hidden = false;
    $("admin-table").replaceChildren();
    $("admin-gate").textContent = "正在重新验证管理员权限…";
  });
  window.addEventListener("pageshow", async (event) => {
    if (!event.persisted) return;
    const own = pageRevision;
    try {
      await A.refresh();
      if (own !== pageRevision) return;
      pageActive = true;
      await table();
    } catch {
      if (own !== pageRevision) return;
      $("admin-gate").textContent = "无法验证登录状态，请刷新页面重试。";
    }
  });
  A.ready.then(() => {
    gate();
    if (A.user?.role === "admin") table();
  });
})();
