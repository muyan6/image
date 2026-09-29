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
    isPressingOriginal: false
  },

  onLoad(options) {
    const rawOrig = options.original ? decodeURIComponent(options.original) : '';
    const rawRes = options.result ? decodeURIComponent(options.result) : '';
    const orig = api.cleanUrl ? api.cleanUrl(rawOrig) : rawOrig;
    const res = api.cleanUrl ? api.cleanUrl(rawRes) : rawRes;
    const quality = options.quality === 'light' ? 'light' : 'fine';
    const demo = !!options.demo || (!orig && !res);

    this._jobId = options.job ? decodeURIComponent(options.job) : '';

    this.setData({
      originalUrl: demo ? DEMO_ORIG : orig,
      resultUrl: demo ? DEMO_RESULT : res,
      quality: quality,
      demo: demo,
      label: quality === 'fine' ? '2K 深度超分' : '1.5K 标准修复'
    });
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

  /** 量取对比舞台的精确宽度 */
  measure() {
    return new Promise((resolve) => {
      wx.createSelectorQuery()
        .select('#stage')
        .boundingClientRect((rect) => {
          if (rect && rect.width > 0) this._rect = rect;
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
