/**
 * 后端接口封装
 *
 * 后端是异步的：POST /api/rescue 立刻返回 job_id，处理在后台跑，
 * 需要轮询 GET /api/jobs/{id} 直到 succeeded / failed。
 * 这样模型推理的 15~30 秒不会撑爆 wx.request 的超时。
 *
 * 光子余额以服务端为准：登录/提交任务的响应都会带 balance，
 * 这里负责同步到 app.globalData，页面只读展示。
 */

const REQUEST_TIMEOUT = 30000;
const UPLOAD_TIMEOUT = 60000;
const POLL_INTERVAL = 1500;
const POLL_TIMEOUT = 180000; // 3 分钟，覆盖精细档最慢的情况

function apiBase() {
  const app = getApp();
  const base = app && app.globalData && app.globalData.apiBase;
  return base || 'https://image.myil.top';
}

/** 保护 COS 预签名直链：剥离被错误拼接的外挂 query 参数（如 &t=, &retry=），防止破坏 HMAC 签名导致 403 */
function cleanUrl(url) {
  if (!url || typeof url !== 'string') return url || '';
  if (url.includes('q-signature=') || url.includes('myqcloud.com')) {
    return url.replace(/([&?])(t|retry)=\d+/g, (m, p) => (p === '?' ? '?' : ''))
              .replace(/\?&/, '?')
              .replace(/[?&]$/, '');
  }
  return url;
}

/** 把后端返回的相对路径补成完整 URL，并保护 COS 预签名不被破坏 */
function absolute(path) {
  if (!path) return '';
  let full = path;
  if (!/^https?:\/\//i.test(path)) {
    full = apiBase() + (path[0] === '/' ? path : '/' + path);
  }
  return cleanUrl(full);
}

function makeError(res) {
  const body = (res && res.data) || {};
  const detail = body.detail;
  const msg = (detail && typeof detail === 'object' ? detail.message : detail) || body.message || ('请求失败 (' + res.statusCode + ')');
  const err = new Error(msg);
  err.status = res.statusCode;
  if (detail && typeof detail === 'object') {
    err.detail = detail;
    syncAccount(detail);
  }
  return err;
}

function networkError(err) {
  const e = new Error((err && err.errMsg) || '网络连接失败');
  e.code = 'NETWORK';
  return e;
}

/** 把提交/登录响应里的余额与免扣费标记同步进全局 */
function syncAccount(data) {
  const app = getApp();
  if (!app || !data) return;
  if (typeof data.balance === 'number') app.setBalance(data.balance);
  if (typeof data.user_id === 'string' && typeof app.setUserIdentity === 'function') app.setUserIdentity(data.user_id);
  if (typeof data.free_mode === 'boolean') app.globalData.freeMode = data.free_mode;
}

const STORAGE_TOKEN = 'sessionToken';
let memoryToken = '';
let tokenStorageSnapshot = null;

function getToken() {
  try {
    const stored = wx.getStorageSync(STORAGE_TOKEN) || '';
    if (memoryToken && stored === tokenStorageSnapshot) return memoryToken;
    // 持久化值主动清除/替换后，不复活上一次登录的内存凭据。
    memoryToken = '';
    tokenStorageSnapshot = null;
    return stored;
  } catch (e) { return memoryToken; }
}

function setToken(token) {
  memoryToken = token;
  try { tokenStorageSnapshot = wx.getStorageSync(STORAGE_TOKEN) || ''; } catch (e) {}
  try {
    wx.setStorageSync(STORAGE_TOKEN, token);
    tokenStorageSnapshot = wx.getStorageSync(STORAGE_TOKEN) || '';
  } catch (e) { /* 存储已满时，本次会话仍使用新登录凭据。 */ }
}

/**
 * 确保已登录：有 token 直接用；没有或 force 时走 wx.login → /api/auth/login。
 */
let loginPromise = null;
function ensureLogin(force) {
  if (loginPromise) return loginPromise;
  if (!force && getToken()) return Promise.resolve(getToken());
  loginPromise = new Promise((resolve, reject) => {
    wx.login({
      success: (r) => {
        if (!r.code) { reject(new Error('微信登录失败')); return; }
        let appId = '';
        try { appId = wx.getAccountInfoSync().miniProgram.appId || ''; } catch (e) {}
        wx.request({
          url: apiBase() + '/api/auth/login',
          method: 'POST',
          data: { code: r.code, app_id: appId },
          header: { 'content-type': 'application/json' },
          timeout: REQUEST_TIMEOUT,
          success(res) {
            if (res.statusCode === 200 && res.data && res.data.token) {
              setToken(res.data.token);
              syncAccount(res.data);
              resolve(res.data.token);
            } else {
              reject(makeError(res));
            }
          },
          fail: (err) => reject(networkError(err))
        });
      },
      fail: () => reject(networkError({ errMsg: 'wx.login 失败' }))
    });
  });
  return loginPromise.finally(() => { loginPromise = null; });
}

/** 带 token 的请求封装：401 时自动重新登录并重试一次 */
function authedCall(fn) {
  return ensureLogin().then(() => {
    const attemptedToken = getToken();
    return fn().catch((err) => {
      if (err && err.code === 'UNAUTHORIZED') {
        const ready = getToken() && getToken() !== attemptedToken
          ? Promise.resolve(getToken()) : ensureLogin(true);
        return ready.then(fn);
      }
      throw err;
    });
  });
}

function request(path, options) {
  const protectedPath = /^\/api\/(me(?:\/|$)|my\/|jobs\/|uploads(?:\/|$)|rescue(?:\/|$)|text-generation(?:\/|$)|payment\/|community\/(?:submissions|posts)(?:\/|$)|auth\/wechat-web\/approve)/.test(path);
  return protectedPath ? authedCall(() => rawRequest(path, options)) : rawRequest(path, options);
}

function rawRequest(path, options) {
  const opts = options || {};
  const billableTextPost = path === '/api/text-generation' && (opts.method || 'GET').toUpperCase() === 'POST';
  return new Promise((resolve, reject) => {
    wx.request({
      url: apiBase() + path,
      method: opts.method || 'GET',
      data: opts.data,
      header: Object.assign(
        { 'content-type': 'application/json' },
        opts.header,
        getToken() ? { 'Authorization': 'Bearer ' + getToken() } : {}
      ),
      timeout: opts.timeout || REQUEST_TIMEOUT,
      success(res) {
        if (res.statusCode >= 200 && res.statusCode < 300) { syncAccount(res.data); resolve(res.data); }
        else {
          const err = makeError(res);
          if (res.statusCode === 401) err.code = 'UNAUTHORIZED';
          if (billableTextPost && res.statusCode >= 500) err.jobSubmissionAttempted = true;
          reject(err);
        }
      },
      fail: (err) => {
        const error = networkError(err);
        if (billableTextPost) error.jobSubmissionAttempted = true;
        reject(error);
      },
    });
  });
}

function downloadImage(url, header) {
  return new Promise((resolve, reject) => {
    wx.downloadFile({
      url, header: header || {}, timeout: UPLOAD_TIMEOUT,
      success(res) {
        if (res.statusCode !== 200 || !res.tempFilePath) {
          const err = new Error(res.statusCode === 404 ? 'COS 图片文件不存在或已到期' :
            res.statusCode === 403 ? 'COS 拒绝访问（403）：签名或访问权限异常' : 'COS 图片下载失败 (' + res.statusCode + ')');
          err.status = res.statusCode;
          reject(err); return;
        }
        // 某些 CDN/代理返回 HTTP 200 错误页，必须确认实际文件能够解码。
        wx.getImageInfo({src: res.tempFilePath,
          success: () => resolve(res.tempFilePath),
          fail: () => reject(new Error('图片数据异常，请重试'))});
      },
      fail: (err) => {
        const raw = (err && err.errMsg) || '';
        const e = networkError(err);
        if (/domain list|domainlist|合法域名/i.test(raw)) {
          e.code = 'DOWNLOAD_DOMAIN';
          e.message = 'COS 下载域名未配置：' + url.split('/')[2];
        } else if (/ssl|tls|certificate|cert\b/i.test(raw)) {
          e.code = 'DOWNLOAD_TLS';
          e.message = 'COS 域名 HTTPS 证书或连接异常';
        }
        reject(e);
      }
    });
  });
}

function isJobCosUrl(url) {
  return /^https:\/\//i.test(url || '') &&
    url.split('/')[2].toLowerCase() !== apiBase().split('/')[2].toLowerCase();
}

/** 已下载临时文件可复用；COS 预签名在临近到期时不能继续当作永久缩略图。 */
function isReusableMediaUrl(url) {
  if (/^(wxfile:|http:\/\/tmp\/)/i.test(url || '')) return true;
  if (!isJobCosUrl(url)) return false;
  const match = /[?&]q-sign-time=([^&]+)/i.exec(url);
  if (!match) return !/[?&]q-signature=/i.test(url);
  try {
    const expiry = Number(decodeURIComponent(match[1]).split(';')[1]);
    return Number.isFinite(expiry) && expiry > Math.floor(Date.now() / 1000) + 30;
  } catch (err) { return false; }
}

/** 只返回直连 COS 的新地址；缺失对象只能经内网补存，不代理图片给客户端。 */
function repairJobMedia(jobId, kind = 'result') {
  if (!jobId || !['orig', 'result'].includes(kind)) return Promise.reject(new Error('图片参数错误'));
  return request('/api/jobs/' + encodeURIComponent(jobId) + '/refresh-media?kind=' + kind,
    {method: 'POST', timeout: UPLOAD_TIMEOUT}).then((data) => {
      if (!isJobCosUrl(data && data.url)) throw new Error('COS 尚未提供可用的图片直链');
      return absolute(data.url);
    });
}

/** 图片始终直连 COS；403 时仅向 API 换新签名重试一次，不代理图片字节。 */
async function downloadJobMedia(jobId, kind, signedUrl) {
  if (!['orig', 'result'].includes(kind)) throw new Error('图片参数错误');
  const remember = (path) => {
    const app = getApp();
    if (app && app.globalData && jobId) {
      app.globalData.mediaCache = app.globalData.mediaCache || {};
      app.globalData.mediaCache[jobId] = app.globalData.mediaCache[jobId] || {};
      app.globalData.mediaCache[jobId][kind] = path;
    }
    return path;
  };
  const directUrl = (url) => {
    // 拒绝历史本地存储地址以及所有 API 同域地址，避免占用业务服务器图片带宽。
    if (!/^https:\/\//i.test(url || '')) throw new Error('此图片尚未同步到 COS，请重新打开作品重试');
    const host = url.split('/')[2].toLowerCase();
    const server = apiBase().split('/')[2].toLowerCase();
    if (host === server) throw new Error('此图片尚未同步到 COS，请重新打开作品重试');
    return cleanUrl(url);
  };
  let url = signedUrl;
  if (!url && jobId) {
    const job = await request('/api/jobs/' + encodeURIComponent(jobId));
    url = job[kind + '_url'];
  }
  const repair = async () => {
    const url = await repairJobMedia(jobId, kind);
    return remember(await downloadImage(directUrl(url)));
  };
  if (jobId && url && (!/^https:\/\//i.test(url) || url.split('/')[2].toLowerCase() === apiBase().split('/')[2].toLowerCase()))
    return repair();
  try { return remember(await downloadImage(directUrl(url))); }
  catch (err) {
    if (jobId && err.status === 404) return repair();
    if (!jobId || (err.status !== 403 && err.status !== 401)) throw err;
    const job = await request('/api/jobs/' + encodeURIComponent(jobId));
    const fresh = job[kind + '_url'];
    if (!fresh) throw new Error(kind === 'orig' ? '原图已到保存期限' : '作品已到保存期限');
    return remember(await downloadImage(directUrl(fresh)));
  }
}

function upload(filePath, formData, options) {
  return authedCall(() => rawUpload(filePath, formData, options));
}

function rawUpload(filePath, formData, options) {
  const opts = options || {};
  return new Promise((resolve, reject) => {
    wx.uploadFile({
      url: apiBase() + '/api/rescue',
      filePath: filePath,
      name: 'image',
      formData: formData || {},
      header: getToken() ? { 'Authorization': 'Bearer ' + getToken() } : {},
      timeout: opts.timeout || UPLOAD_TIMEOUT,
      success(res) {
        let data;
        try {
          data = JSON.parse(res.data);
        } catch (e) {
          const err = new Error('服务器返回格式错误');
          err.jobSubmissionAttempted = res.statusCode >= 200 && res.statusCode < 300 || res.statusCode >= 500;
          reject(err);
          return;
        }
        if (res.statusCode >= 200 && res.statusCode < 300) {
          syncAccount(data);
          resolve(data);
        } else {
          const err = makeError({data, statusCode: res.statusCode});
          if (res.statusCode === 401) err.code = 'UNAUTHORIZED';
          if (res.statusCode >= 500) err.jobSubmissionAttempted = true;
          reject(err);
        }
      },
      fail: (err) => {
        const error = networkError(err);
        error.jobSubmissionAttempted = true;
        reject(error);
      },
    });
  });
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * 轮询任务直到结束。
 * @param {string} jobId
 * @param {{interval?:number, timeout?:number, onTick?:(job:object)=>void}} options
 * @returns {Promise<object>} 成功时返回最终的 job
 */
async function waitForJob(jobId, options) {
  const opts = options || {};
  const interval = opts.interval || POLL_INTERVAL;
  const timeout = opts.timeout || POLL_TIMEOUT;
  const deadline = Date.now() + timeout;
  let lastStatus = null;
  let delay = interval;

  while (Date.now() < deadline) {
    if (opts.isCanceled && opts.isCanceled()) {
      const err = new Error('用户已切换页面，后台继续处理');
      err.code = 'USER_BACKGROUND';
      throw err;
    }
    try {
      const job = await request('/api/jobs/' + jobId);
      // 云端任务也按前台轮询周期显示进度，不再因部署方式额外等待 5 秒。
      delay = interval;
      if (opts.isCanceled && opts.isCanceled()) {
        const err = new Error('任务已转入后台'); err.code = 'USER_BACKGROUND'; throw err;
      }
      if (job.status + ':' + job.stage !== lastStatus) {
        lastStatus = job.status + ':' + job.stage;
        if (opts.onTick) opts.onTick(job);
      }
      if (job.status === 'succeeded') return job;
      if (job.status === 'failed') {
        const err = new Error(job.error || '修图失败，请重试');
        err.code = 'JOB_FAILED';
        if(job.violation)err.detail=job.violation;
        throw err;
      }
    } catch (e) {
      if (e && (e.code === 'JOB_FAILED' || e.code === 'USER_BACKGROUND' ||
          e.code === 'UNAUTHORIZED' || (e.status && e.status < 500 && e.status !== 429))) throw e;
      const msg = String((e && e.message) || '');
      // 用户切屏或小程序进入后台，微信可能抛 request:fail canceled / abort
      if (opts.isCanceled && opts.isCanceled()) {
        const err = new Error('用户已切换页面，后台继续处理');
        err.code = 'USER_BACKGROUND';
        throw err;
      }
      if (msg.includes('canceled') || msg.includes('abort')) {
        const err = new Error('切屏已转入后台，任务在云端继续运行');
        err.code = 'USER_BACKGROUND';
        throw err;
      }
    }
    await sleep(Math.min(delay, Math.max(0, deadline - Date.now())));
  }

  const err = new Error('处理超时，请稍后在历史记录中查看');
  err.code = 'TIMEOUT';
  throw err;
}

/** 查询当前用户云端最近提交的任务历史列表 */
function myJobs(limit = 30, offset = 0) {
  return request('/api/my/jobs?limit=' + limit + (offset ? '&offset=' + offset : ''), { timeout: 8000 });
}

/** 后端连通性探测 */
function health() {
  return request('/api/health', { timeout: 5000 });
}

/** 价格 / 维护状态 / 风格表 / 直传开关（后台可改，启动时拉一次即可） */
let configPromise = null;
function config() {
  // 仅合并正在飞行的请求，不缓存已完成配置；后台改价/维护开关仍及时生效。
  if (!configPromise) configPromise = request('/api/config', { timeout: 5000 })
    .finally(() => { configPromise = null; });
  return configPromise;
}

/** 公告列表（只含启用的，最新在前） */
function announcements() {
  return request('/api/announcements', { timeout: 5000 });
}

/** 模板商店：分组 + 启用的模板（封面是 COS 直链或后端封面路径） */
function templates() {
  return request('/api/templates', { timeout: 8000 });
}

/** 当前用户资料：光子余额、邀请码、今日奖励进度 */
function me() {
  return request('/api/me');
}

function updateProfile(nickname) {
  return request('/api/me/profile', {method:'POST', data:{nickname}});
}

/** 每日奖励：kind = 'checkin'（1次/天）| 'video'（3次/天），服务端记账 */
function earn(kind, receipt) {
  return request('/api/me/earn', {
    method: 'POST', data: { kind: kind, receipt: receipt || '' }
  });
}

/** 绑定邀请码（每人一次，双方 +30 光子） */
function bindInvite(code) {
  return request('/api/me/invite', {
    method: 'POST', data: { code: code }
  });
}

function submitViolationFeedback(violationId, message) {
  return request('/api/me/violation-feedback', {
    method: 'POST', data: { violation_id: violationId, message }
  });
}

/* ------------------------- COS 直传三件套 ------------------------- */

function createUpload(filename, byteSize) {
  return request('/api/uploads', {
    method: 'POST',
    data: { filename: filename || 'photo.jpg', byte_size: byteSize || 0 }
  });
}

function putToCos(url, arrayBuffer) {
  return new Promise((resolve, reject) => {
    wx.request({
      url: url,
      method: 'PUT',
      data: arrayBuffer,
      header: { 'content-type': 'application/octet-stream' },
      timeout: UPLOAD_TIMEOUT,
      success(res) {
        if (res.statusCode >= 200 && res.statusCode < 300) resolve(true);
        else reject(new Error('直传失败 (' + res.statusCode + ')'));
      },
      fail: (err) => reject(networkError(err))
    });
  });
}

function completeUpload(uploadId) {
  return request('/api/uploads/' + uploadId + '/complete', {
    method: 'POST', data: {}
  });
}

function rescueByUpload(payload) {
  return request('/api/rescue/by-upload', {
    method: 'POST', data: payload, timeout: UPLOAD_TIMEOUT
  });
}

/** multipart 里的表单值必须是字符串 */
function _stringifyFormData(formData) {
  const out = {};
  Object.keys(formData || {}).forEach((k) => {
    const v = formData[k];
    out[k] = typeof v === 'string' ? v : JSON.stringify(v);
  });
  return out;
}

/** COS上传前失败可回退；任务提交结果不确定时不重复提交。 */
async function _submitViaCos(filePath, formData) {
  const fs = wx.getFileSystemManager();
  const stat = fs.statSync(filePath);
  const extMatch = /\.(\w+)$/.exec(filePath || '');
  const ext = extMatch ? '.' + extMatch[1].toLowerCase() : '.jpg';

  const read = new Promise((resolve, reject) => {
    fs.readFile({
      filePath: filePath,
      success: (r) => resolve(r.data),
      fail: (e) => reject(networkError(e))
    });
  });
  const [up, buf] = await Promise.all([createUpload('photo' + ext, stat.size || 0), read]);
  await putToCos(up.url, buf);
  await completeUpload(up.upload_id);

  const body = {};
  ['quality', 'style', 'template_id', 'text_fields', 'aspect_ratio', 'custom_prompt', 'template_output_mode', 'expected_price'].forEach((k) => {
    if (formData && formData[k] !== undefined && formData[k] !== '') {
      body[k] = formData[k];
    }
  });
  body.upload_id = up.upload_id;
  try {
    return await rescueByUpload(body);
  } catch (err) {
    // The server may already have accepted a timed-out request; don't submit twice.
    if (!err.status || err.status >= 500) err.jobSubmissionAttempted = true;
    throw err;
  }
}

/**
 * 统一任务提交入口：后端 cos_ready 时优先 COS 直传（图片字节不过服务器），
 * 上传链路故障可回退multipart；业务拒绝或疑似已受理的提交不自动重交。
 * 成功时响应里的 balance 已同步进 app.globalData。
 */
async function submitJob(filePath, formData) {
  const form = _stringifyFormData(formData);
  // 登录与公开配置互不依赖，并行准备；任一失败都不开始上传。
  const [, cfg] = await Promise.all([ensureLogin(), config()]);
  if(form.template_id&&(!cfg||!Array.isArray(cfg.template_quality_options)||!cfg.template_quality_options.includes('light')||!cfg.template_quality_options.includes('fine'))){
    const e=new Error('模板双档需更新后端后启用');e.status=503;throw e;
  }
  if(form.expected_price!==undefined){
    const price=cfg.free_mode?0:cfg.prices&&cfg.prices[form.quality||'light'];
    if(!Number.isInteger(price)||Number(form.expected_price)!==price){const e=new Error('生成价格已更新，请刷新价格后重新确认');e.status=409;throw e;}
  }
  if (cfg && cfg.cos_ready) {
      try { return await _submitViaCos(filePath, form); }
      catch (err) {
        if (cfg.cloud_pipeline && cfg.cloud_pipeline.enabled) throw err;
      if (err.jobSubmissionAttempted || (err.status && err.status < 500)) throw err;
      console.warn('COS 上传链路异常，回退 multipart：', err);
    }
    }
    if (cfg.cloud_pipeline && cfg.cloud_pipeline.enabled) throw new Error('云端 COS 上传通道未配置，请稍后重试');
    return upload(filePath, form);
}

function deleteJob(jobId) {
  return request('/api/my/jobs/' + encodeURIComponent(jobId), {method: 'DELETE'});
}
function deleteAllJobs() {
  return request('/api/my/jobs', {method: 'DELETE'});
}

module.exports = {
  apiBase,
  absolute,
  cleanUrl,
  request,
  isJobCosUrl,
  isReusableMediaUrl,
  repairJobMedia,
  downloadJobMedia,
  upload,
  submitJob,
  waitForJob,
  myJobs,
  deleteJob,
  deleteAllJobs,
  health,
  config,
  announcements,
  templates,
  ensureLogin,
  authedCall,
  me,
  updateProfile,
  earn,
  bindInvite,
  submitViolationFeedback,
  createUpload,
  putToCos,
  completeUpload,
  rescueByUpload,
  POLL_INTERVAL,
  POLL_TIMEOUT,
};
