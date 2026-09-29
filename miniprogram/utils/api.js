/**
 * 后端接口封装
 *
 * 后端是异步的：POST /api/rescue 立刻返回 job_id，处理在后台跑，
 * 需要轮询 GET /api/jobs/{id} 直到 succeeded / failed。
 * 这样模型推理的 15~30 秒不会撑爆 wx.request 的超时。
 */

const REQUEST_TIMEOUT = 30000;
const UPLOAD_TIMEOUT = 60000;
const POLL_INTERVAL = 1500;
const POLL_TIMEOUT = 180000; // 3 分钟，覆盖精细档最慢的情况

function apiBase() {
  const app = getApp();
  const base = app && app.globalData && app.globalData.apiBase;
  return base || 'http://127.0.0.1:8000';
}

/** 把后端返回的相对路径补成完整 URL */
function absolute(path) {
  if (!path) return '';
  if (/^https?:\/\//i.test(path)) return path;
  return apiBase() + (path[0] === '/' ? path : '/' + path);
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
function ensureLogin(force) {
  if (!force && getToken()) return Promise.resolve(getToken());
  return new Promise((resolve, reject) => {
    wx.login({
      success: (r) => {
        if (!r.code) { reject(new Error('微信登录失败')); return; }
        wx.request({
          url: apiBase() + '/api/auth/login',
          method: 'POST',
          data: { code: r.code },
          header: { 'content-type': 'application/json' },
          timeout: REQUEST_TIMEOUT,
          success(res) {
            if (res.statusCode === 200 && res.data && res.data.token) {
              setToken(res.data.token);
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
}

/** 带 token 的请求封装：401 时自动重新登录并重试一次 */
function authedCall(fn) {
  return ensureLogin().then(() =>
    fn().catch((err) => {
      if (err && err.code === 'UNAUTHORIZED') {
        return ensureLogin(true).then(fn);
      }
      throw err;
    })
  );
}

function request(path, options) {
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
        if (res.statusCode >= 200 && res.statusCode < 300) resolve(res.data);
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
    const job = await request('/api/jobs/' + jobId);
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
    await sleep(interval);
  }

  const err = new Error('处理超时，请稍后在历史记录中查看');
  err.code = 'TIMEOUT';
  throw err;
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

/** COS 直传三件套 */
function createUpload(filename, byteSize) {
  return authedCall(() => request('/api/uploads', {
    method: 'POST',
    data: { filename: filename || 'photo.jpg', byteSize: byteSize || 0 }
  }));
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
  return authedCall(() => request('/api/uploads/' + uploadId + '/complete', {
    method: 'POST', data: {}
  }));
}

function rescueByUpload(payload) {
  return authedCall(() => request('/api/rescue/by-upload', {
    method: 'POST', data: payload, timeout: UPLOAD_TIMEOUT
  }));
}

module.exports = {
  apiBase,
  absolute,
  request,
  upload,
  waitForJob,
  health,
  config,
  announcements,
  templates,
  ensureLogin,
  authedCall,
  createUpload,
  putToCos,
  completeUpload,
  rescueByUpload,
  POLL_INTERVAL,
  POLL_TIMEOUT,
};