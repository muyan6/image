const app = getApp();
const api = require('../../utils/api.js');

Page({
  data: {
    imagePath: '',
    templateId: '',
    selectedTemplate: null,
    allTemplates: [],
    
    // 画幅设置 (截图2)
    currentRatioKey: '1:1',
    currentRatioLabel: '1:1',
    previewStyle: 'height: 694rpx;',
    showRatioModal: false,
    ratioOptions: [
      { key: '1:1', label: '1:1', desc: '经典方形构图，社交分享与人像首选', rec: true },
      { key: 'original', label: '保持原图', desc: '保留原始拍摄长宽比例，不进行裁剪', rec: false },
      { key: '3:4', label: '3:4', desc: '复古肖像与半身人像经典比例', rec: false },
      { key: '4:3', label: '4:3', desc: '传统横版构图与风景纪实', rec: false },
      { key: '9:16', label: '9:16', desc: '全屏手机壁纸与竖版视觉', rec: false },
      { key: '16:9', label: '16:9', desc: '宽屏电影画幅构图', rec: false }
    ],

    // 档位与价格 (截图3)
    quality: 'fine', // 'light' | 'fine'
    costLight: 80,
    costFine: 120,
    currentQualityCost: 120,
    priceRangeText: '80~120 积分',

    // 用户与权益
    fishTokens: 90,
    freeMode: false,
    rightsOk: true, // 默认打勾，符合截图3

    // 状态
    processing: false,
    processingText: '正在提交照片…'
  },

  onLoad(options) {
    const img = options.image ? decodeURIComponent(options.image) : '';
    const tid = options.templateId ? decodeURIComponent(options.templateId) : '';
    
    this.setData({
      imagePath: img,
      templateId: tid
    });

    this.loadUserData();
    this.loadTemplatesData(tid);
  },

  onShow() {
    this.loadUserData();
  },

  loadUserData() {
    const tokens = app.globalData.fishTokens != null ? app.globalData.fishTokens : 90;
    this.setData({
      fishTokens: tokens
    });

    api.config().then((c) => {
      if (!c) return;
      const free = !!c.free_mode;
      const prices = c.prices || {};
      // 如果后端配置了 light/fine 价格，折算为积分展示 (若 price < 10 比如 1, 3 则放大到截图同款 80, 120)
      const pLight = (prices.light != null) ? (prices.light >= 10 ? prices.light : prices.light * 80) : 80;
      const pFine = (prices.fine != null) ? (prices.fine >= 10 ? prices.fine : prices.fine * 40) : 120;
      
      const currentCost = this.data.quality === 'fine' ? pFine : pLight;
      this.setData({
        freeMode: free,
        costLight: pLight,
        costFine: pFine,
        currentQualityCost: currentCost,
        priceRangeText: `${pLight}~${pFine} 积分`
      });
    }).catch(() => {});
  },

  loadTemplatesData(selectedTid) {
    api.templates().then((d) => {
      const items = (d && d.items) || [];
      const list = items.map((t) => Object.assign({}, t, {
        coverUrl: t.cover ? api.absolute(t.cover) : '/images/logo.jpg'
      }));

      let current = list.find((t) => t.id === selectedTid);
      if (!current && list.length > 0) {
        current = list[0];
      }

      this.setData({
        allTemplates: list,
        selectedTemplate: current
      });
    }).catch(() => {});
  },

  /** 切换画幅比例 */
  onOpenRatioModal() {
    this.setData({ showRatioModal: true });
  },

  onCloseRatioModal() {
    this.setData({ showRatioModal: false });
  },

  onSelectRatioOption(e) {
    const key = e.currentTarget.dataset.key;
    const label = e.currentTarget.dataset.label;
    const heightMap = {
      '1:1': 'height: 694rpx;',
      '3:4': 'height: 925rpx;',
      '4:3': 'height: 520rpx;',
      '9:16': 'height: 1230rpx;',
      '16:9': 'height: 390rpx;',
      'original': 'height: 694rpx;'
    };
    this.setData({
      currentRatioKey: key,
      currentRatioLabel: label,
      previewStyle: heightMap[key] || 'height: 694rpx;',
      showRatioModal: false
    });
  },

  /** 切换风格 (截图3) */
  onSelectTemplate(e) {
    const tpl = e.currentTarget.dataset.template;
    if (!tpl) return;
    this.setData({
      selectedTemplate: tpl,
      templateId: tpl.id
    });
    try {
      wx.vibrateShort({ type: 'light' });
    } catch (err) {}
  },

  /** 切换生成档位 (截图3) */
  onSelectQuality(e) {
    const q = e.currentTarget.dataset.quality;
    const cost = q === 'fine' ? this.data.costFine : this.data.costLight;
    this.setData({
      quality: q,
      currentQualityCost: cost
    });
    try {
      wx.vibrateShort({ type: 'light' });
    } catch (err) {}
  },

  /** 切换版权勾选 (截图3) */
  onToggleRights() {
    this.setData({
      rightsOk: !this.data.rightsOk
    });
  },

  /** 查看协议 */
  onOpenProtocol(e) {
    const type = e.currentTarget.dataset.type;
    const titles = {
      service: '服务协议',
      privacy: '隐私指引',
      auth: '照片授权说明'
    };
    const contents = {
      service: '本服务运用前沿人工智能图像算法对您上传的照片进行艺术重绘与画质重构。生成内容由模型运算产生，请合法合规使用。',
      privacy: '我们极其重视您的隐私安全。您上传的照片仅在处理任务期间用于算法推理与合成，不作任何商业展示或第三方披露，您可随时在历史记录中删除。',
      auth: '您确认对上传的照片拥有完整使用权或已获得权利人合法授权，绝不上传包含未授权他人肖像、侵权、色情低俗或违反法律法规的内容。'
    };
    wx.showModal({
      title: titles[type] || '说明',
      content: contents[type] || '',
      showCancel: false,
      confirmText: '我知道了'
    });
  },

  /** 点击【开始生成】(截图2/3) */
  async onStartGenerate() {
    if (this.data.processing) return;

    if (!this.data.rightsOk) {
      wx.showToast({
        title: '请先确认照片使用权',
        icon: 'none'
      });
      return;
    }

    const cost = this.data.currentQualityCost;
    if (!this.data.freeMode && this.data.fishTokens < cost) {
      wx.showModal({
        title: '算力余额不足',
        content: `本次生成需要 ${cost} 积分，当前剩余 ${this.data.fishTokens} 积分。是否前往补给？`,
        confirmText: '补充算力',
        cancelText: '取消',
        success: (r) => {
          if (r.confirm) {
            wx.navigateTo({ url: '/pages/credits/credits' });
          }
        }
      });
      return;
    }

    this.setData({
      processing: true,
      processingText: '正在提交照片…'
    });

    const filePath = this.data.imagePath;
    wx.compressImage({
      src: filePath,
      quality: 90,
      success: (res) => this.executeUpload(res.tempFilePath || filePath),
      fail: () => this.executeUpload(filePath)
    });
  },

  async executeUpload(path) {
    const quality = this.data.quality;
    const tpl = this.data.selectedTemplate;
    const ratio = this.data.currentRatioKey;

    try {
      const formData = {
        quality: quality,
        aspect_ratio: ratio
      };
      if (tpl && tpl.id) {
        formData.template_id = tpl.id;
      }

      const created = await api.upload(path, formData);
      if (!created || created.code !== 0 || !created.job_id) {
        throw new Error((created && created.detail) || '服务响应异常');
      }

      this.setData({
        processingText: `正在进行${tpl ? tpl.name : 'AI'}风格重构…`
      });

      const job = await api.waitForJob(created.job_id, {
        onTick: (j) => {
          if (j.stage === 'enhance') {
            this.setData({ processingText: 'AI 深度重构光影与细节中…' });
          }
        }
      });

      // 扣费
      const cost = this.data.currentQualityCost;
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
        templateName: tpl ? tpl.name : '',
        jobId: created.job_id,
        time: this.formatTime(new Date())
      };
      app.globalData.historyList = app.globalData.historyList || [];
      app.globalData.historyList.unshift(historyItem);
      if (app.globalData.historyList.length > 50) app.globalData.historyList.length = 50;
      app.persist();

      this.setData({ processing: false });

      // 跳转至全屏拖拽滑块对比页
      wx.redirectTo({
        url: `/pages/compare/compare?original=${encodeURIComponent(origUrl)}&result=${encodeURIComponent(bustUrl)}&quality=${quality}`
      });
    } catch (err) {
      console.error('生成失败', err);
      this.setData({ processing: false });
      wx.showModal({
        title: '生成未完成',
        content: err.message || '网络连接超时，请重试',
        showCancel: false,
        confirmText: '确定'
      });
    }
  },

  formatTime(date) {
    const pad = (n) => (n < 10 ? '0' + n : '' + n);
    return `${date.getFullYear()}.${pad(date.getMonth() + 1)}.${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }
});
