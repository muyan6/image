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
    const orig = options.original ? decodeURIComponent(options.original) : '';
    const res = options.result ? decodeURIComponent(options.result) : '';
    const quality = options.quality === 'light' ? 'light' : 'fine';
    const demo = !!options.demo || (!orig && !res);

    this.setData({
      originalUrl: demo ? DEMO_ORIG : orig,
      resultUrl: demo ? DEMO_RESULT : res,
      quality: quality,
      demo: demo,
      label: quality === 'fine' ? '2K 深度超分' : '1.5K 标准修复'
    });
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
    if (!url || this.data.demo || /[?&]retry=/.test(url)) return;
    const sep = url.indexOf('?') >= 0 ? '&' : '?';
    this.setData({ resultUrl: url + sep + 'retry=' + Date.now() });
  },

  onOrigError() {
    console.warn('原图加载失败');
  },

  /** 保存高清修复照片到系统相册 */
  onDownload() {
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

    if (/^https?:\/\//i.test(url)) {
      const cleanUrl = url.split('?')[0];
      wx.downloadFile({
        url: cleanUrl,
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
      this.saveToAlbum(url, done);
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
