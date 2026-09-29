const app = getApp();
const api = require('../../utils/api.js');

Page({
  data: {
    state: 'empty', // 'empty' | 'ready' | 'processing' | 'result'
    selectedImage: '',
    selectedImageSize: '',
    currentQuality: 'fine',
    currentStyle: '',
    priceLight: 1,
    priceFine: 3,
    fishTokens: 0,
    freeMode: false,
    processingText: '正在拯救这张照片…',
    resultImage: '',
    originalImage: '',
    splitPercent: 50,
    isDragging: false,
    historyList: [],
    notice: null,
    showCreditModal: false,
    showHistoryModal: false,
    saving: false,
    featuredTemplates: [],
    activeTemplate: null
  },

  onLoad() {
    this.loadNotice();
    this.loadConfig();
    this.loadTemplates();
  },

  onShow() {
    this.setData({
      fishTokens: app.globalData.fishTokens,
      historyList: (app.globalData.historyList || [])
    });

    // 检查是否有跨页面携带过来的模板 (例如在模板库或社区点了"做同款")
    if (app.globalData.selectedTemplate) {
      const tpl = app.globalData.selectedTemplate;
      app.globalData.selectedTemplate = null;
      this.setData({
        activeTemplate: tpl,
        currentQuality: tpl.engine === 'fine' ? 'fine' : 'light'
      });
      if (this.data.state === 'empty') {
        this.onPickImage();
      }
    }
  },

  loadTemplates() {
    api.templates()
      .then((d) => {
        const items = (d && d.items) || [];
        const featured = items.slice(0, 5).map((t) => {
          return Object.assign({}, t, {
            coverUrl: t.cover ? api.absolute(t.cover) : '/images/logo.jpg'
          });
        });
        if (featured.length > 0) {
          this.setData({ featuredTemplates: featured });
        }
      })
      .catch(() => {});
  },

  onGoToTemplates() {
    wx.switchTab({
      url: '/pages/templates/templates'
    });
  },

  onSelectFeaturedTemplate(e) {
    const tpl = e.currentTarget.dataset.template;
    this.setData({
      activeTemplate: tpl,
      currentQuality: tpl.engine === 'fine' ? 'fine' : 'light'
    });
    if (this.data.state === 'empty') {
      this.onPickImage();
    } else {
      wx.showToast({ title: '已选用：' + tpl.name, icon: 'none' });
    }
  },

  onClearTemplate() {
    this.setData({ activeTemplate: null });
  },

  loadNotice() {
    api.announcements()
      .then((d) => {
        const items = (d && d.items) || [];
        this.setData({ notice: items.length ? items[0] : null });
      })
      .catch(() => {});
  },

  onTapNotice() {
    const n = this.data.notice;
    if (!n) return;
    wx.showModal({
      title: n.title || '系统公告',
      content: n.body || '',
      showCancel: false,
      confirmText: '我知道了'
    });
  },

  loadConfig() {
    api.config()
      .then((c) => {
        if (!c) return;
        const p = c.prices || {};
        this.setData({
          priceLight: (p.light != null) ? p.light : this.data.priceLight,
          priceFine: (p.fine != null) ? p.fine : this.data.priceFine,
          freeMode: !!c.free_mode
        });
      })
      .catch(() => {});
  },

  /** 状态 1：选择照片入口 */
  onPickImage() {
    if (this.data.state === 'processing') return;

    wx.chooseMedia({
      count: 1,
      mediaType: ['image'],
      sourceType: ['album', 'camera'],
      sizeType: ['compressed', 'original'],
      success: (res) => {
        const file = res.tempFiles[0];
        const sizeMb = (file.size / (1024 * 1024)).toFixed(2);
        this.setData({
          selectedImage: file.tempFilePath,
          selectedImageSize: sizeMb > 0 ? `${sizeMb} MB` : '',
          state: 'ready',
          resultImage: '',
          originalImage: '',
          splitPercent: 50
        });
      }
    });
  },

  /** 切换画质档位 */
  onSelectQuality(e) {
    if (this.data.state === 'processing') return;
    const q = e.currentTarget.dataset.quality;
    this.setData({ currentQuality: q });
  },

  /** 重新选图或换一张 */
  onChangePhoto() {
    if (this.data.state === 'processing') return;
    this.setData({
      state: 'empty',
      selectedImage: '',
      selectedImageSize: '',
      resultImage: '',
      originalImage: '',
      splitPercent: 50
    });
  },

  /** 状态 2：点击【拯救这张照片】开始处理 */
  onRescue() {
    if (this.data.state === 'processing' || !this.data.selectedImage) return;

    const quality = this.data.currentQuality;
    const cost = quality === 'fine' ? this.data.priceFine : this.data.priceLight;

    if (!this.data.freeMode && this.data.fishTokens < cost) {
      wx.showModal({
        title: '算力不足',
        content: `本次修复需要 ${cost} 点算力，当前剩余 ${this.data.fishTokens} 点。是否立即补充？`,
        confirmText: '免费补给',
        cancelText: '取消',
        success: (r) => {
          if (r.confirm) this.onOpenCreditModal();
        }
      });
      return;
    }

    this.setData({
      state: 'processing',
      processingText: '正在提交照片…'
    });

    wx.compressImage({
      src: this.data.selectedImage,
      quality: 88,
      success: (r) => this.doUpload(r.tempFilePath || this.data.selectedImage),
      fail: () => this.doUpload(this.data.selectedImage)
    });
  },

  async doUpload(filePath) {
    const quality = this.data.currentQuality;
    const tpl = this.data.activeTemplate;
    const qualityLabel = tpl ? `${tpl.name}风格重构` : (quality === 'fine' ? '精细 2K 超分重构' : '轻量 1.5K 快速修复');

    try {
      const formData = { quality };
      if (tpl && tpl.id) {
        formData.template_id = tpl.id;
      }
      const created = await api.upload(filePath, formData);
      if (!created || created.code !== 0 || !created.job_id) {
        throw new Error((created && created.detail) || '服务响应异常');
      }

      this.setData({
        processingText: `正在进行${qualityLabel}…`
      });

      const job = await api.waitForJob(created.job_id, {
        onTick: (j) => {
          if (j.stage === 'enhance') {
            this.setData({ processingText: 'AI 深度重构光影与细节中…' });
          }
        }
      });

      // 扣费
      let cost = quality === 'fine' ? this.data.priceFine : this.data.priceLight;
      if (tpl && typeof tpl.price === 'number') {
        cost = tpl.price;
      }
      if (!this.data.freeMode && cost > 0) {
        app.consumeToken(cost);
      }

      const origUrl = api.absolute(job.orig_url || created.orig_url);
      const resUrl = api.absolute(job.result_url || created.result_url);
      const bustUrl = resUrl + (resUrl.indexOf('?') >= 0 ? '&' : '?') + 't=' + Date.now();

      // 记录历史
      const historyItem = {
        original: origUrl,
        result: bustUrl,
        quality: quality,
        jobId: created.job_id,
        time: this.formatTime(new Date())
      };
      app.globalData.historyList.unshift(historyItem);
      if (app.globalData.historyList.length > 50) app.globalData.historyList.length = 50;
      app.persist();

      this.setData({
        state: 'result',
        originalImage: origUrl,
        resultImage: bustUrl,
        fishTokens: app.globalData.fishTokens,
        historyList: app.globalData.historyList,
        splitPercent: 50
      });

      try {
        wx.vibrateShort({ type: 'medium' });
      } catch (e) {}

    } catch (err) {
      this.setData({ state: 'ready' });
      if (err.code === 'NETWORK') {
        wx.showModal({
          title: '网络连接失败',
          content: '后端服务未开启，进入演示对比模式？',
          confirmText: '体验演示',
          success: (r) => {
            if (r.confirm) wx.navigateTo({ url: '/pages/compare/compare?demo=1' });
          }
        });
        return;
      }
      wx.showModal({
        title: '拯救未完成',
        content: err.message || '请稍后重试',
        showCancel: false,
        confirmText: '我知道了'
      });
    }
  },

  /** 对比舞台尺寸测量与滑块拖动 */
  measure() {
    return new Promise((resolve) => {
      wx.createSelectorQuery()
        .select('#photo-stage')
        .boundingClientRect((rect) => {
          if (rect && rect.width > 0) this._rect = rect;
          resolve(this._rect || null);
        })
        .exec();
    });
  },

  onTouchStart(e) {
    if (this.data.state !== 'result') return;
    this.setData({ isDragging: true });
    this.measure().then(() => this.moveTo(e));
  },

  onTouchMove(e) {
    if (this.data.state !== 'result') return;
    this.moveTo(e);
  },

  onTouchEnd() {
    this.setData({ isDragging: false });
  },

  moveTo(e) {
    const touch = e.touches && e.touches[0];
    const rect = this._rect;
    if (!rect || !touch) return;
    const raw = ((touch.clientX - rect.left) / rect.width) * 100;
    const pct = Math.max(0, Math.min(100, Math.round(raw)));
    if (pct === this.data.splitPercent) return;
    this.setData({ splitPercent: pct });
  },

  /** 保存修复后的照片到相册 */
  onSave() {
    const url = this.data.resultImage;
    if (!url || this.data.saving) return;

    this.setData({ saving: true });
    wx.showLoading({ title: '正在保存照片…', mask: true });

    const done = () => {
      this.setData({ saving: false });
      wx.hideLoading();
    };

    const cleanUrl = url.split('?')[0];
    wx.downloadFile({
      url: cleanUrl,
      success: (res) => {
        if (res.statusCode === 200 && res.tempFilePath) {
          wx.saveImageToPhotosAlbum({
            filePath: res.tempFilePath,
            success: () => {
              done();
              wx.showToast({ title: '已保存至相册', icon: 'success' });
            },
            fail: (err) => {
              done();
              const msg = (err && err.errMsg) || '';
              if (msg.indexOf('auth') >= 0) {
                wx.showModal({
                  title: '需要相册权限',
                  content: '请在设置中允许访问相册',
                  confirmText: '去授权',
                  success: (r) => { if (r.confirm) wx.openSetting(); }
                });
              } else if (msg.indexOf('cancel') < 0) {
                wx.showToast({ title: '保存失败', icon: 'none' });
              }
            }
          });
        } else {
          done();
          wx.showToast({ title: '下载失败', icon: 'none' });
        }
      },
      fail: () => {
        done();
        wx.showToast({ title: '下载失败', icon: 'none' });
      }
    });
  },

  /** 全屏暗房深度对比 */
  onOpenFullscreen() {
    wx.navigateTo({
      url: '/pages/compare/compare?original=' + encodeURIComponent(this.data.originalImage) +
        '&result=' + encodeURIComponent(this.data.resultImage) +
        '&quality=' + this.data.currentQuality
    });
  },

  /** 积分中心页面 */
  onOpenCreditModal() {
    wx.navigateTo({
      url: '/pages/credits/credits'
    });
  },

  onCloseCreditModal() {
    this.setData({ showCreditModal: false });
  },

  onGainCredits() {
    app.addToken(3);
    this.setData({
      fishTokens: app.globalData.fishTokens,
      showCreditModal: false
    });
    wx.showToast({ title: '已领取 +3 算力', icon: 'success' });
  },

  /** 我的作品管理页面 */
  onOpenHistory() {
    wx.navigateTo({
      url: '/pages/works/works'
    });
  },

  onCloseHistory() {
    this.setData({ showHistoryModal: false });
  },

  onSelectHistoryItem(e) {
    const idx = e.currentTarget.dataset.index;
    const item = this.data.historyList[idx];
    if (!item) return;

    this.setData({
      showHistoryModal: false,
      state: 'result',
      originalImage: item.original,
      resultImage: item.result,
      currentQuality: item.quality || 'fine',
      splitPercent: 50
    });
  },

  onClearHistory() {
    wx.showModal({
      title: '清空历史',
      content: '确定要清空本地所有修复记录吗？',
      confirmColor: '#9e4b3c',
      success: (r) => {
        if (r.confirm) {
          app.clearHistory();
          this.setData({ historyList: [], showHistoryModal: false });
          wx.showToast({ title: '已清空', icon: 'success' });
        }
      }
    });
  },

  formatTime(date) {
    const m = date.getMonth() + 1;
    const d = date.getDate();
    const h = date.getHours().toString().padStart(2, '0');
    const min = date.getMinutes().toString().padStart(2, '0');
    return `${m}月${d}日 ${h}:${min}`;
  }
});