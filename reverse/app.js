"use strict";
const $ = (id) => document.getElementById(id);
const ui = Object.fromEntries(
  [
    "main",
    "title",
    "upload-area",
    "file-input",
    "photo-panel",
    "photo-stage",
    "original",
    "result",
    "result-layer",
    "photo-labels",
    "comparison",
    "processing",
    "message",
    "actions",
    "rescue",
    "rescue-text",
    "save",
    "change",
    "footnote",
  ].map((id) => [id, $(id)]),
);
const LIMIT = 25 * 1024 * 1024;
const RAW_LIMIT = 100 * 1024 * 1024;
const RAW_EXTENSION =
  /\.(dng|cr2|cr3|crw|nef|nrw|arw|srf|sr2|raf|orf|ori|rw2|rwl|pef|ptx|srw|3fr|fff|iiq|kdc|dcr|mos|mrw|raw|x3f|erf|mef)$/i;
let rawLoading = false;
let previewRequest = null;
let state = "empty";
let quality = "fine";
let file = null;
let originalURL = null;
let resultURL = null;
let request = null;
let revision = 0;
let selection = 0;
let imageRatio = 1.5;
let split = 50;
let dragging = false;
let immersive = false;
let activeJob = null;
let pollTimer = null;
let pendingSubmission = null;
let pendingRaw = null;
let selectedUploadId = null;
const Account = window.Account;
const revealMotion = {};
let renderedState = null;
let processingText = "正在提交照片…";
const processingLabel = document.querySelector(".processing-status > span:last-child");
function phase(text) {
  processingText = text;
  processingLabel.textContent = text;
}

function message(text = "", error = false) {
  ui.message.textContent = text;
  ui.message.classList.toggle("error", error);
  if (immersive) updateSize();
}

function render() {
  const stateChanged = renderedState !== state;
  const previousState = renderedState;
  renderedState = state;
  ui.main.dataset.state = state;
  document.body.classList.toggle("has-photo", state !== "empty");
  document.body.classList.toggle("is-immersive", immersive);
  const empty = state === "empty";
  const busy = state === "processing";
  const done = state === "result";
  const back = $("return-home");
  back.hidden = empty && !rawLoading;
  back.disabled = !!pendingSubmission || (busy && !activeJob);
  back.title = back.disabled ? "请先恢复修复，确认本次提交结果" : "返回首页；已提交的任务可从账户修复记录中查看";
  document.getElementById("quality-picker").hidden = empty || done || busy;
  document.querySelectorAll("[data-quality]").forEach((button) => {
    button.disabled = busy || rawLoading || !!pendingSubmission;
    button.setAttribute(
      "aria-pressed",
      String(button.dataset.quality === quality),
    );
  });
  updateSize();
  ui["upload-area"].hidden = !empty;
  ui["photo-panel"].hidden = empty;
  ui.actions.hidden = empty || busy;
  ui.rescue.hidden = done || (!file && state === "error");
  ui.rescue.disabled = busy || rawLoading;
  ui["upload-area"].disabled = rawLoading;
  document.querySelector(".upload-title").textContent = rawLoading
    ? "正在读取 RAW…"
    : "选择照片";
  ui["rescue-text"].textContent = busy
    ? "正在拯救…"
    : pendingSubmission && state === "error"
      ? "恢复修复"
      : state === "error"
        ? "重新拯救"
        : "拯救这张照片";
  ui.change.disabled = busy || rawLoading || !!pendingSubmission;
  ui.save.hidden = !done;
  ui.processing.hidden = !busy;
  ui["result-layer"].hidden = !done;
  ui["photo-labels"].hidden = !done;
  ui.comparison.hidden = !done;
  ui["photo-panel"].setAttribute("aria-busy", String(busy));
  processingLabel.textContent = processingText;
  if (stateChanged && previousState !== null) {
    if (state === "ready") UI.enter(ui["photo-panel"], { duration: 420, scale: 0.97, y: 20 });
    if (state === "empty") {
      UI.enter(document.querySelector(".intro"), { duration: 420, y: 16 });
      UI.enter(document.querySelector(".upload-entry"), { duration: 420, y: 24 });
    }
    UI.enter(ui.actions, { duration: 320, y: 16 });
    UI.enter($("quality-picker"));
    if (busy) UI.enter(ui.processing, { duration: 320 });
  }
  ui.title.replaceChildren("让这一刻，");
  const titleBreak = document.createElement("br");
  titleBreak.className = "mobile-break";
  ui.title.append(titleBreak, "重获新生。");
  ui.footnote.textContent = done
    ? `请检查修复细节，照片保留至 ${new Date(activeJob.expiresAt).toLocaleString("zh-CN", { hour12: false })}。`
    : busy
      ? "正在后台修复，关页后可从账户继续查看。"
      : state === "error"
        ? file
          ? "原图已保留，可重试或换一张。"
          : "可以换一张照片继续修复。"
        : "点击拯救后，照片才会发送至 AI 修复服务。";
  updateSize();
}

function updateSize() {
  const top = immersive ? Math.ceil(document.querySelector(".site-header").getBoundingClientRect().height) + 8 : 0;
  const bottom = immersive ? Math.max(window.innerHeight < 500 ? 104 : 176,
    Math.ceil(document.querySelector(".below-photo").getBoundingClientRect().height)) + 8 : 0;
  const maxHeight = immersive
    ? Math.max(60, window.innerHeight - top - bottom)
    : Math.min(320, Math.max(180, window.innerHeight - 350));
  ui["photo-panel"].style.setProperty("--canvas-top", `${top}px`);
  ui["photo-panel"].style.setProperty("--canvas-bottom", `${bottom}px`);
  ui["photo-panel"].style.height = `${maxHeight}px`;
  ui["photo-stage"].style.setProperty(
    "--photo-width",
    `${maxHeight * imageRatio}px`,
  );
  ui["photo-stage"].style.setProperty("--photo-ratio", String(imageRatio));
  document.body.classList.toggle(
    "compact-view",
    immersive && window.innerHeight < 500,
  );
}

function setSplit(value) {
  split = Math.max(0, Math.min(100, value));
  ui["photo-stage"].style.setProperty("--split", `${split}%`);
  ui.comparison.setAttribute("aria-valuenow", String(Math.round(split)));
  ui.comparison.setAttribute(
    "aria-valuetext",
    `原图 ${Math.round(split)}%，修复后 ${100 - Math.round(split)}%`,
  );
}

async function checkType(candidate) {
  const b = new Uint8Array(await candidate.slice(0, 16).arrayBuffer());
  return (
    (b[0] === 255 && b[1] === 216 && b[2] === 255) ||
    (b[0] === 137 &&
      b[1] === 80 &&
      b[2] === 78 &&
      b[3] === 71 &&
      b[4] === 13 &&
      b[5] === 10 &&
      b[6] === 26 &&
      b[7] === 10) ||
    (String.fromCharCode(...b.slice(0, 4)) === "RIFF" &&
      String.fromCharCode(...b.slice(8, 12)) === "WEBP")
  );
}

async function selectFiles(files) {
  if (state === "processing" || rawLoading || pendingSubmission) return;
  const list = Array.from(files);
  if (list.length !== 1) {
    message("请每次选择一张照片。", true);
    return;
  }
  const candidate = list[0];
  const current = ++selection;
  selectedUploadId = null;
  if (RAW_EXTENSION.test(candidate.name || "")) {
    await Account.ready;
    if (current !== selection || state === "processing" || rawLoading || pendingSubmission) return;
    if (!Account.requireLogin()) {
      pendingRaw = candidate;
      ui["file-input"].value = "";
      return;
    }
  }
  let url;
  try {
    const isRaw = RAW_EXTENSION.test(candidate.name || "");
    if (candidate.size > (isRaw ? RAW_LIMIT : LIMIT))
      throw new Error(
        isRaw
          ? "RAW 照片不能超过 100 MB。"
          : "照片不能超过 25 MB，请换一张较小的照片。",
      );
    let displayFile = candidate;
    if (isRaw) {
      rawLoading = true;
      const controller = new AbortController();
      previewRequest = controller;
      const timer = setTimeout(() => controller.abort(), 75_000);
      render();
      message("正在解码 RAW，原片不会发送到 AI 服务。");
      try {
        let blob;
        if (Account.config.objectStorage) {
          const completed = await uploadToObjectStorage(candidate, "raw-preview", controller.signal);
          const response = await fetch(completed.previewUrl, {
            signal: controller.signal, credentials: "omit", cache: "no-store", referrerPolicy: "no-referrer",
          });
          if (!response.ok || !response.headers.get("content-type")?.startsWith("image/png"))
            throw new Error("RAW 预览不完整，请重试。");
          blob = await response.blob();
          selectedUploadId = completed.uploadId;
        } else {
          const body = new FormData();
          body.append("image", candidate, candidate.name);
          const response = await Account.request("/api/preview", {
            method: "POST", binary: true, body, signal: controller.signal,
            headers: { "X-Photo-Preview": "1" },
          });
          if (!response.ok) {
            const data = await response.json().catch(() => null);
            throw new Error(data?.error?.message || "RAW 读取失败，请换一张照片。");
          }
          if (!response.headers.get("content-type")?.startsWith("image/png"))
            throw new Error("RAW 预览不完整，请重试。");
          blob = await response.blob();
        }
        controller.signal.throwIfAborted();
        displayFile = new File(
          [blob],
          candidate.name.replace(/\.[^.]+$/, ".png"),
          { type: "image/png" },
        );
      } finally {
        clearTimeout(timer);
        if (previewRequest === controller) previewRequest = null;
      }
    } else if (!(await checkType(candidate)))
      throw new Error("请选择 JPG、PNG、WebP 或相机 RAW 照片。");
    if (current !== selection) return;
    url = URL.createObjectURL(displayFile);
    const preview = new Image();
    preview.src = url;
    await preview.decode();
    if (
      !preview.naturalWidth ||
      !preview.naturalHeight ||
      preview.naturalWidth * preview.naturalHeight > 60_000_000
    )
      throw new Error("照片像素过大，请缩小后重试。");
    if (current !== selection || state === "processing") {
      URL.revokeObjectURL(url);
      return;
    }
    revision++;
    if (originalURL) URL.revokeObjectURL(originalURL);
    if (resultURL) URL.revokeObjectURL(resultURL);
    originalURL = url;
    resultURL = null;
    file = displayFile;
    imageRatio = preview.naturalWidth / preview.naturalHeight;
    ui.original.src = originalURL;

    ui.result.removeAttribute("src");
    ui.save.removeAttribute("href");
    updateSize();
    state = "ready";
    activeJob = null;
    immersive = false;
    message();
    render();
    ui.rescue.focus({ preventScroll: true });
  } catch (error) {
    if (url) URL.revokeObjectURL(url);
    if (current === selection)
      message(
        error.name === "AbortError"
          ? "RAW 读取已中断或超时，请重试。"
          : error instanceof DOMException
            ? "无法读取这张照片，请换一个完整的图片文件。"
            : error.message,
        true,
      );
  } finally {
    if (current === selection) {
      rawLoading = false;
      ui["file-input"].value = "";
      render();
    }
  }
}

async function uploadToObjectStorage(photo, purpose, signal) {
  const intent = await Account.request("/api/uploads", {
    method: "POST", body: {
      filename: photo.name || "照片.jpg",
      mimeType: photo.type || "application/octet-stream",
      byteSize: photo.size,
      purpose,
    }, signal,
  });
  const put = await fetch(intent.url, {
    method: "PUT", body: photo, signal, credentials: "omit",
    cache: "no-store", referrerPolicy: "no-referrer",
  });
  if (!put.ok) throw new Error("照片直传失败，请重试上传。");
  return Account.request(`/api/uploads/${intent.uploadId}/complete`, {
    method: "POST", body: {}, signal,
  });
}

function expandPhoto() {
  const before = ui["photo-stage"].getBoundingClientRect();
  immersive = true;
  state = "processing";
  message();
  render();
  const after = ui["photo-stage"].getBoundingClientRect();
  if (
    before.width &&
    after.width &&
    ui["photo-stage"].animate &&
    !window.matchMedia?.("(prefers-reduced-motion: reduce)").matches
  ) {
    UI.animate(ui["photo-stage"],
      [
        {
          transformOrigin: "0 0",
          transform: `translate(${before.left - after.left}px, ${before.top - after.top}px) scale(${before.width / after.width}, ${before.height / after.height})`,
          borderRadius: "6px",
        },
        { transformOrigin: "0 0", transform: "none", borderRadius: "0" },
      ],
      { duration: 600, easing: UI.ease },
    );
  }
}

async function rescue() {
  if (!file || state === "processing" || state === "result" || rawLoading)
    return;
  await Account.ready;
  if (!file || state === "processing" || state === "result") return;
  if (!Account.requireLogin()) return;
  if (
    !pendingSubmission &&
    Account.user.available < Account.config.prices[quality]
  ) {
    Account.open("redeem");
    message("小鱼干不足，请先兑换卡密。", true);
    return;
  }
  const ownRevision = ++revision;
  selection++;
  pendingSubmission ||= {
    id: crypto.randomUUID(),
    file,
    quality,
    quoteVersion: Account.config.version,
    uploadId: selectedUploadId,
  };
  const current = pendingSubmission,
    controller = new AbortController();
  request = controller;
  const timer = setTimeout(() => controller.abort(), 70000);
  phase("正在提交照片…");
  expandPhoto();
  try {
    let body;
    if (Account.config.objectStorage) {
      if (!current.uploadId) {
        phase("正在直传照片…");
        const completed = await uploadToObjectStorage(current.file, "rescue", controller.signal);
        current.uploadId = completed.uploadId;
      }
      body = { requestId: current.id, uploadId: current.uploadId,
        quality: current.quality, quoteVersion: String(current.quoteVersion) };
    } else {
      body = new FormData();
      body.append("image", current.file, current.file.name || "photo.png");
      body.append("quality", current.quality);
      body.append("quoteVersion", String(current.quoteVersion));
    }
    phase("正在提交修复任务…");
    const data = await Account.request("/api/rescue", {
      method: "POST", body, signal: controller.signal,
      headers: { "X-Rescue-Request-Id": current.id },
    });
    if (ownRevision !== revision) return;
    pendingSubmission = null;
    activeJob = data.job;
    render();
    phase("正在后台修复…");
    Account.assign({ user: data.user });
    pollJob(ownRevision);
  } catch (error) {
    if (ownRevision !== revision) return;
    if (
      error.code &&
      (error.status < 500 ||
        ["NOT_CONFIGURED", "STOPPING"].includes(error.code))
    )
      pendingSubmission = null;
    if (error.code === "PRICE_CHANGED") await Account.refresh().catch(() => {});
    if (ownRevision !== revision) return;
    state = "error";
    message(
      pendingSubmission
        ? "提交状态尚未确认。点击恢复修复查询原请求，不会重复扣小鱼干。"
        : error.message,
      true,
    );
    render();
    if (error.code === "INSUFFICIENT_CREDITS") Account.open("redeem");
  } finally {
    clearTimeout(timer);
    if (request === controller) request = null;
  }
}

async function imageBlob(path, signal) {
  const match = /^\/api\/jobs\/([0-9a-f-]{36})\/(original|result)$/.exec(path);
  if (match && Account.config.objectStorage) {
    const media = await Account.request(`/api/jobs/${match[1]}/media-url?kind=${match[2]}`, { signal });
    if (media.url) {
      const response = await fetch(media.url, {
        signal, credentials: "omit", cache: "no-store", referrerPolicy: "no-referrer",
      });
      if (!response.ok) throw new Error("照片读取失败，请重新加载。");
      return response.blob();
    }
  }
  const response = await Account.request(path, { binary: true, signal });
  return response.blob();
}
async function showJobResult(job, ownRevision, signal) {
  phase("正在读取修复结果…");
  const blobs = await Promise.all([
    imageBlob(`/api/jobs/${job.id}/original`, signal),
    imageBlob(`/api/jobs/${job.id}/result`, signal),
  ]);
  const urls = blobs.map((blob) => URL.createObjectURL(blob));
  try {
    await Promise.all(
      urls.map(async (url) => {
        const img = new Image();
        img.src = url;
        await img.decode();
      }),
    );
    if (ownRevision !== revision) return;
    if (originalURL) URL.revokeObjectURL(originalURL);
    if (resultURL) URL.revokeObjectURL(resultURL);
    [originalURL, resultURL] = urls;
    ui.original.src = originalURL;
    ui.result.src = resultURL;
    imageRatio = job.width / job.height;
    ui.save.href = resultURL;
    ui.save.download = `${job.filename.replace(/\.[^.]+$/, "")}-已拯救.jpg`;
    state = "result";
    UI.cancel(revealMotion);
    setSplit(UI.reduced() ? 50 : 100);
    message();
    render();
    UI.tween(revealMotion, split, 50, 650, setSplit);
    UI.enter(ui["photo-labels"], { duration: 320 });
    if (!document.querySelector("dialog[open]")) ui.comparison.focus({ preventScroll: true });
  } finally {
    for (const url of urls)
      if (url !== originalURL && url !== resultURL) URL.revokeObjectURL(url);
  }
}
async function pollJob(ownRevision) {
  clearTimeout(pollTimer);
  if (!activeJob || ownRevision !== revision || !Account.user) return;
  const controller = new AbortController();
  request = controller;
  const timeout = setTimeout(() => controller.abort(), 15000);
  try {
    const data = await Account.request(`/api/jobs/${activeJob.id}`, {
      signal: controller.signal,
    });
    if (ownRevision !== revision) return;
    activeJob = data.job;
    render();
    phase("正在后台修复…");
    Account.assign({ user: data.user });
    message();
    if (activeJob.status === "running") phase("正在后台修复…");
    if (activeJob.status === "succeeded") {
      await showJobResult(activeJob, ownRevision, controller.signal);
      return;
    }
    if (
      !originalURL &&
      activeJob.status !== "expired" &&
      (!activeJob.expiresAt || activeJob.expiresAt > Date.now())
    ) {
      try {
        const blob = await imageBlob(
          `/api/jobs/${activeJob.id}/original`,
          controller.signal,
        );
        if (ownRevision !== revision) return;
        originalURL = URL.createObjectURL(blob);
        ui.original.src = originalURL;
        file = new File(
          [blob],
          activeJob.filename.replace(/\.[^.]+$/, "") + ".png",
          { type: "image/png" },
        );
        imageRatio = activeJob.width / activeJob.height;
        render();
      } catch (error) {
        if (error.status !== 409 && error.status !== 410) throw error;
      }
    }
    if (["failed", "expired"].includes(activeJob.status)) {
      state = "error";
      pendingSubmission = null;
      message(
        activeJob.error?.message || "照片已超过 30 天保留期限，已删除。",
        true,
      );
      render();
      return;
    }
    pollTimer = setTimeout(() => pollJob(ownRevision), 2000);
  } catch (error) {
    if (ownRevision !== revision) return;
    if (error.code === "UNAUTHORIZED") {
      Account.open();
      return;
    }
    if ([403, 404, 410].includes(error.status)) {
      state = "error";
      message(error.message, true);
      render();
      return;
    }
    phase("连接中断，正在重新连接…");
    message("后台任务仍会继续，连接恢复后将自动显示结果。");
    pollTimer = setTimeout(() => pollJob(ownRevision), 5000);
  } finally {
    clearTimeout(timeout);
    if (request === controller) request = null;
  }
}
async function restoreJob(id) {
  await Account.ready;
  if (!Account.requireLogin()) return;
  if (pendingSubmission) {
    message("请先点击恢复修复，确认本次提交结果后再查看其他照片。", true);
    return;
  }
  reset(true);
  const ownRevision = ++revision;
  try {
    const data = await Account.request(`/api/jobs/${id}`);
    if (ownRevision !== revision) return;
    activeJob = data.job;
    quality = activeJob.quality;
    imageRatio = activeJob.width / activeJob.height;
    if (activeJob.status === "expired") {
      message("照片已超过 30 天保留期限，已删除。", true);
      return;
    }
    immersive = true;
    state = "processing";
    phase("正在恢复修复任务…");
    render();
    pollJob(ownRevision);
  } catch (error) {
    if (ownRevision === revision) message(error.message, true);
  }
}
async function recoverRunning() {
  if (!Account.user || file || activeJob) return;
  const ownRevision = revision;
  try {
    const data = await Account.request("/api/jobs");
    if (ownRevision !== revision || file || activeJob) return;
    const job = data.items.find((j) => j.status === "running");
    if (job) restoreJob(job.id);
  } catch {
    /* The account panel can retry recovery. */
  }
}

function reset(force = false) {
  if (!force && (state === "processing" || rawLoading || pendingSubmission))
    return;
  UI.cancel(revealMotion);
  UI.cancel(ui["photo-stage"]);
  UI.cancel(ui["photo-panel"]);
  UI.cancel(ui.processing);
  dragging = false;
  clearTimeout(pollTimer);
  previewRequest?.abort();
  rawLoading = false;
  activeJob = null;
  pendingSubmission = null;
  pendingRaw = null;
  revision++;
  selection++;
  request?.abort();
  if (originalURL) URL.revokeObjectURL(originalURL);
  if (resultURL) URL.revokeObjectURL(resultURL);
  file = originalURL = resultURL = null;
  selectedUploadId = null;

  UI.cancel(revealMotion);
  ui.original.removeAttribute("src");
  ui.result.removeAttribute("src");
  ui.save.removeAttribute("href");
  ui["file-input"].value = "";
  state = "empty";
  immersive = false;
  message();
  render();
  ui["upload-area"].focus({ preventScroll: true });
}

ui["upload-area"].addEventListener("click", () => ui["file-input"].click());
ui["file-input"].addEventListener("change", (event) => {
  if (event.target.files.length) selectFiles(event.target.files);
});
ui.rescue.addEventListener("click", rescue);
ui.change.addEventListener("click", () => reset());
$("return-home").addEventListener("click", () => {
  if (pendingSubmission || (state === "processing" && !activeJob)) return;
  const running = state === "processing";
  reset(true);
  if (running) message("任务仍在后台修复，可从账户的修复记录中继续查看。");
});
document.querySelector(".brand").addEventListener("click", event => {
  if (state === "empty" && !rawLoading) return;
  event.preventDefault();
  $("return-home").click();
});
ui.save.addEventListener("click", () => message("下载已发起，请在浏览器下载列表中查看。"));
document.addEventListener("dragover", (event) => {
  event.preventDefault();
  if (
    state !== "processing" &&
    !rawLoading &&
    !document.querySelector("dialog[open]") &&
    event.dataTransfer?.types.includes("Files")
  )
    document.body.classList.add("is-dragging");
});
document.addEventListener("dragleave", (event) => {
  if (!event.relatedTarget) document.body.classList.remove("is-dragging");
});
document.addEventListener("drop", (event) => {
  event.preventDefault();
  document.body.classList.remove("is-dragging");
  if (document.querySelector("dialog[open]")) return;
  if (event.dataTransfer.files.length) selectFiles(event.dataTransfer.files);
});
document.addEventListener("paste", (event) => {
  if (document.querySelector("dialog[open]")) return;
  const files = [...(event.clipboardData?.items || [])]
    .filter((item) => item.kind === "file")
    .map((item) => item.getAsFile())
    .filter(Boolean);
  if (files.length) {
    event.preventDefault();
    selectFiles(files);
  }
});
function positionFromPointer(event) {
  UI.cancel(revealMotion);
  const rect = ui.comparison.getBoundingClientRect();
  setSplit(((event.clientX - rect.left) / rect.width) * 100);
}
ui.comparison.addEventListener("pointerdown", (event) => {
  if (event.button !== 0 || !event.isPrimary) return;
  dragging = true;
  ui.comparison.classList.add("is-dragging");
  ui.comparison.setPointerCapture(event.pointerId);
  ui.comparison.focus({ preventScroll: true });
  positionFromPointer(event);
});
ui.comparison.addEventListener("pointermove", (event) => {
  if (dragging) positionFromPointer(event);
});
for (const type of ["pointerup", "pointercancel", "lostpointercapture"])
  ui.comparison.addEventListener(type, () => {
    dragging = false;
    ui.comparison.classList.remove("is-dragging");
  });
ui.comparison.addEventListener("keydown", (event) => {
  UI.cancel(revealMotion);
  const step = event.shiftKey ? 10 : 2;
  const values = {
    ArrowLeft: split - step,
    ArrowDown: split - step,
    ArrowRight: split + step,
    ArrowUp: split + step,
    Home: 0,
    End: 100,
  };
  if (event.key in values) {
    event.preventDefault();
    setSplit(values[event.key]);
  }
});
window.addEventListener("resize", updateSize);
if (window.ResizeObserver) {
  const layoutObserver = new ResizeObserver(updateSize);
  layoutObserver.observe(document.querySelector(".site-header"));
  layoutObserver.observe(document.querySelector(".below-photo"));
}
window.addEventListener("pagehide", () => {
  UI.cancel(revealMotion);
  revision++;
  selection++;
  clearTimeout(pollTimer);
  request?.abort();
  previewRequest?.abort();
  if (rawLoading) {
    rawLoading = false;
    message("RAW 读取已中断，请重新选择照片。", true);
  }
  if (pendingSubmission && !activeJob) {
    state = "error";
    message("提交状态尚未确认。点击恢复修复查询原请求，不会重复扣小鱼干。", true);
  }
  render();
});
window.addEventListener("pageshow", (event) => {
  if (event.persisted)
    Account.refresh()
      .then(() => {
        if (activeJob) pollJob(revision);
        else recoverRunning();
      })
      .catch(() => {});
});
window.addEventListener("restorejob", (event) => restoreJob(event.detail));
window.addEventListener("accountchange", (event) => {
  if (
    event.detail.previousId &&
    event.detail.previousId !== event.detail.user?.id
  )
    reset(true);
  if (event.detail.user && pendingRaw) {
    const candidate = pendingRaw;
    pendingRaw = null;
    selectFiles([candidate]);
  } else if (event.detail.user && !event.detail.previousId) recoverRunning();
});
Account.ready.then(recoverRunning);
fetch("/api/health")
  .then((response) => response.json())
  .then((health) => {
    if (!health.configured && state === "empty")
      message("修复服务尚未配置，请使用启动脚本启动。", true);
  })
  .catch(() => {
    if (state === "empty")
      message("未连接到本地服务，请使用启动脚本打开网站。", true);
  });
render();

document.querySelectorAll("[data-quality]").forEach((button) => {
  button.addEventListener("click", () => {
    if (state === "processing" || rawLoading) return;
    quality = button.dataset.quality;
    render();
  });
});
