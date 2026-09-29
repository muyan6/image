const app = getApp();
const api = require('../../utils/api.js');

const DEMO_ORIG = 'https://images.unsplash.com/photo-1534438327276-14e5300c3a48?w=800';
const DEMO_RESULT = 'https://images.unsplash.com/photo-1534438327276-14e5300c3a48?w=800&q=80';

Page({
  data: {
    originalUrl: '',
    resultUrl: '',
    quality: 'fine',
    splitPercent: 50,
    demo: false,
    saving: false,
    label: '2K 深度超分',
    isDragging: false,
    isPressingOriginal: false,
    // 舞台实测宽度(px)：clip-mat 内层原画按整块舞台锁定，避免裁剪后重新缩放
    stageW: 0
  },

  onLoad(options) {
    const rawOrig = options.original ? decodeURIComponent(options.original) : '';
    const rawRes = options.result ? decodeURIComponent(options.result) : '';
    const orig = api.cleanUrl ? api.cleanUrl(rawOrig) : rawOrig;
    const res = api.cleanUrl ? api.cleanUrl(rawRes) : rawRes;
    const quality = options.quality === 'light' ? 'light' : 'fine';
    const demo = !!options.demo || (!orig && !res);

    this._jobId = options.job ? decodeURIComponent(options.job) : '';

    // 首帧兜底：onReady 的实测尺寸回来之前，先用窗口尺寸撑住，
    // 否则内层原图 width:0 会整张不显示，出现半屏空白
    let winW = 375;
    try {
      const info = wx.getWindowInfo ? wx.getWindowInfo() : wx.getSystemInfoSync();
      winW = info.windowWidth || winW;
    } catch (e) { /* 取不到就用默认值，onReady 会立刻纠正 */ }

    this.setData({
      originalUrl: demo ? DEMO_ORIG : orig,
      resultUrl: demo ? DEMO_RESULT : res,
      quality: quality,
      demo: demo,
      label: quality === 'fine' ? '2K 深度超分' : '1.5K 标准修复',
      stageW: winW
    });

    // COS 直链有效期只有 2 小时（后端 cos_presign ttl_seconds=7200）。
    // 从作品集点进来时，链接往往是几小时前存的，直接渲染会 403/空白。
    // 这里主动换一次新鲜签名（依赖 JobStore 已落盘，重启后仍能查到任务）。
    if (!demo) this.refreshUrls();
  },

  /**
   * 向服务端换一张新的结果图直链（COS 签名 2 小时过期，老图会 403）。
   * 有 jobId 才能刷新；成功后把新地址写回，失败保持原样。
   */
  refreshUrls() {
    if (!this._jobId || this.data.demo) return Promise.resolve(false);
    return api.request('/api/jobs/' + this._jobId)
      .then((job) => {
        if (!job || job.status !== 'succeeded' || !job.result_url) return false;
        const orig = api.absolute(job.orig_url || this.data.originalUrl);
        const res = api.absolute(job.result_url);
        this.setData({ originalUrl: orig, resultUrl: res });
        return true;
      })
      .catch(() => false);
  },

  onReady() {
    this.measure();
  },

  /**
   * 量取对比舞台的精确尺寸。
   *
   * stageW 会写进 data，作为 clip-mat 内层原画的固定像素宽度（高度交给 CSS 100%，
   * 与底层结果图同源，不会出现兜底值与真实值不一致的中间帧）：
   * 原图与结果图必须共享同一个 aspectFit 缩放基准（整块舞台），
   * 否则被裁到一半的原图会按「可见宽度」重新缩放，左右两半高度对不上。
   */
  measure() {
    return new Promise((resolve) => {
      wx.createSelectorQuery()
        .select('#stage')
        .boundingClientRect((rect) => {
          if (rect && rect.width > 0) {
            this._rect = rect;
            if (rect.width !== this.data.stageW) {
              this.setData({ stageW: rect.width });
            }
          }
          resolve(this._rect || null);
        })
        .exec();
    });
  },

  onTouchStart(e) {
    this.setData({ isDragging: true });
    try {
      wx.vibrateShort({ type: 'light' });
    } catch (err) {}
    this.measure().then(() => this.moveTo(e));
  },

  onTouchMove(e) {
    this.moveTo(e);
  },

  onTouchEnd() {
    this.setData({ isDragging: false });
  },

  /** 左右拖动滑块 */
  moveTo(e) {
    const touch = e.touches && e.touches[0];
    const rect = this._rect;
    if (!rect || !touch) return;

    const raw = ((touch.clientX - rect.left) / rect.width) * 100;
    const percent = Math.max(0, Math.min(100, Math.round(raw)));
    if (percent === this.data.splitPercent) return;
    this.setData({ splitPercent: percent });
  },

  /** 长按即时查看原图 (Lightroom 交互) */
  onPressOriginalStart() {
    try {
      wx.vibrateShort({ type: 'light' });
    } catch (err) {}
    this.setData({ isPressingOriginal: true });
  },

  onPressOriginalEnd() {
    this.setData({ isPressingOriginal: false });
  },

  onChangePhoto() {
    wx.navigateBack();
  },

  onClose() {
    wx.navigateBack();
  },

  onResultError() {
    const url = this.data.resultUrl;
    if (!url || this.data.demo || this._refreshingResult) return;
    this._refreshingResult = true;
    this.refreshUrls().finally(() => {
      this._refreshingResult = false;
    });
  },

  onOrigError() {
    const url = this.data.originalUrl;
    if (!url || this.data.demo || this._refreshingOrig) return;
    this._refreshingOrig = true;
    this.refreshUrls().finally(() => {
      this._refreshingOrig = false;
    });
  },

  /** 保存高清修复照片到系统相册 */
  async onDownload() {
    const url = this.data.resultUrl;
    if (!url || this.data.saving) return;
    if (this.data.demo) {
      wx.showToast({ title: '演示模式下无本地原画可保存', icon: 'none' });
      return;
    }

    try {
      wx.vibrateShort({ type: 'medium' });
    } catch (err) {}

    this.setData({ saving: true });
    wx.showLoading({ title: '正在导出原画...', mask: true });

    const done = () => {
      this.setData({ saving: false });
      wx.hideLoading();
    };

    // 直链可能已过签名时效：下载前先向服务端换一次新鲜地址
    await this.refreshUrls();
    // 注意：COS 预签名地址的查询串就是鉴权本身，绝不能剥掉
    const target = this.data.resultUrl;
    if (/^https?:\/\//i.test(target)) {
      wx.downloadFile({
        url: target,
        success: (res) => {
          if (res.statusCode === 200 && res.tempFilePath) {
            this.saveToAlbum(res.tempFilePath, done);
          } else {
            done();
            wx.showToast({ title: '下载失败 (' + res.statusCode + ')', icon: 'none' });
          }
        },
        fail: () => {
          done();
          wx.showToast({ title: '网络连接超时', icon: 'none' });
        }
      });
    } else {
      this.saveToAlbum(target, done);
    }
  },

  saveToAlbum(filePath, done) {
    wx.saveImageToPhotosAlbum({
      filePath: filePath,
      success: () => {
        done();
        wx.showToast({ title: '已保存至相册', icon: 'success' });
      },
      fail: (err) => {
        const msg = (err && err.errMsg) || '';
        if (/suffix|extension|后缀|format/i.test(msg) && !/\.jpg$/i.test(filePath)) {
          const fsm = wx.getFileSystemManager();
          const fixed = wx.env.USER_DATA_PATH + '/rescue_' + Date.now() + '.jpg';
          try {
            fsm.copyFileSync(filePath, fixed);
            this.saveToAlbum(fixed, done);
            return;
          } catch (e) {}
        }
        done();
        if (msg.indexOf('auth deny') >= 0 || msg.indexOf('auth denied') >= 0) {
          wx.showModal({
            title: '需要相册权限',
            content: '请允许访问您的相册以便保存高清修复照片。',
            confirmText: '前往授权',
            success: (r) => { if (r.confirm) wx.openSetting(); }
          });
        } else if (msg.indexOf('cancel') < 0) {
          wx.showToast({ title: '保存失败，请重试', icon: 'none' });
        }
      }
    });
  },

  onShareAppMessage() {
    return {
      title: '看我用 AI 拯救的照片，画质太惊艳了！',
      path: '/pages/index/index'
    };
  }
});
