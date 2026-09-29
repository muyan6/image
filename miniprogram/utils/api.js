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
  const msg = body.detail || body.message || ('请求失败 (' + res.statusCode + ')');
  const err = new Error(msg);
  err.status = res.statusCode;
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

function getToken() {
  try { return wx.getStorageSync(STORAGE_TOKEN) || ''; } catch (e) { return ''; }
}

function setToken(token) {
  try { wx.setStorageSync(STORAGE_TOKEN, token); } catch (e) { /* 存储失败下次重登 */ }
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
  const protectedPath = /^\/api\/(me(?:\/|$)|my\/|jobs\/|uploads(?:\/|$)|rescue(?:\/|$))/.test(path);
  return protectedPath ? authedCall(() => rawRequest(path, options)) : rawRequest(path, options);
}

function rawRequest(path, options) {
  const opts = options || {};
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
          reject(err);
        }
      },
      fail: (err) => reject(networkError(err)),
    });
  });
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
          reject(new Error('服务器返回格式错误'));
          return;
        }
        if (res.statusCode >= 200 && res.statusCode < 300) {
          syncAccount(data);
          resolve(data);
        } else {
          const err = new Error(data.detail || ('上传失败 (' + res.statusCode + ')'));
          err.status = res.statusCode;
          if (res.statusCode === 401) err.code = 'UNAUTHORIZED';
          reject(err);
        }
      },
      fail: (err) => reject(networkError(err)),
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

  while (Date.now() < deadline) {
    if (opts.isCanceled && opts.isCanceled()) {
      const err = new Error('用户已切换页面，后台继续处理');
      err.code = 'USER_BACKGROUND';
      throw err;
    }
    try {
      const job = await request('/api/jobs/' + jobId);
      if (opts.isCanceled && opts.isCanceled()) {
        const err = new Error('任务已转入后台'); err.code = 'USER_BACKGROUND'; throw err;
      }
      if (job.status !== lastStatus) {
        lastStatus = job.status;
        if (opts.onTick) opts.onTick(job);
      }
      if (job.status === 'succeeded') return job;
      if (job.status === 'failed') {
        const err = new Error(job.error || '修图失败，请重试');
        err.code = 'JOB_FAILED';
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
    await sleep(interval);
  }

  const err = new Error('处理超时，请稍后在历史记录中查看');
  err.code = 'TIMEOUT';
  throw err;
}

/** 查询当前用户云端最近提交的任务历史列表 */
function myJobs(limit = 30) {
  return request('/api/my/jobs?limit=' + limit, { timeout: 8000 });
}

/** 后端连通性探测 */
function health() {
  return request('/api/health', { timeout: 5000 });
}

/** 价格 / 维护状态 / 风格表 / 直传开关（后台可改，启动时拉一次即可） */
function config() {
  return request('/api/config', { timeout: 5000 });
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

  const up = await createUpload('photo' + ext, stat.size || 0);
  const buf = await new Promise((resolve, reject) => {
    fs.readFile({
      filePath: filePath,
      success: (r) => resolve(r.data),
      fail: (e) => reject(networkError(e))
    });
  });
  await putToCos(up.url, buf);
  await completeUpload(up.upload_id);

  const body = {};
  ['quality', 'style', 'template_id', 'text_fields', 'aspect_ratio'].forEach((k) => {
    if (formData && formData[k] !== undefined && formData[k] !== '') {
      body[k] = formData[k];
    }
  });
  body.upload_id = up.upload_id;
  try {
    return await rescueByUpload(body);
  } catch (err) {
    // The server may already have accepted a timed-out request; don't submit twice.
    err.jobSubmissionAttempted = true;
    throw err;
  }
}

/**
 * 统一任务提交入口：后端 cos_ready 时优先 COS 直传（图片字节不过服务器），
 * 上传链路故障可回退multipart；业务拒绝或疑似已受理的提交不自动重交。
 * 成功时响应里的 balance 已同步进 app.globalData。
 */
async function submitJob(filePath, formData) {
  await ensureLogin();
  const form = _stringifyFormData(formData);
  let cfg = null;
  try { cfg = await config(); } catch (e) { /* multipart may still be reachable */ }
  if (cfg && cfg.cos_ready) {
    try { return await _submitViaCos(filePath, form); }
    catch (err) {
      if (err.jobSubmissionAttempted || (err.status && err.status < 500)) throw err;
      console.warn('COS 上传链路异常，回退 multipart：', err);
    }
  }
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
  earn,
  bindInvite,
  createUpload,
  putToCos,
  completeUpload,
  rescueByUpload,
  POLL_INTERVAL,
  POLL_TIMEOUT,
};
