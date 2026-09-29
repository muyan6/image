const app = getApp();
const api = require('../../utils/api.js');

Page({
  data: {
    imagePath: '',
    templateId: '',
    selectedTemplate: null,
    allTemplates: [],

    // 画幅设置
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

    // 档位与价格：与后端口径一致（价格就是光子数，不再另行折算）
    quality: 'fine',
    costLight: 1,
    costFine: 3,
    currentQualityCost: 3,
    priceRangeText: '1~3 光子',

    // 模板文字排版字段（模板非空时展示，值随任务提交给后端排版引擎）
    textValues: {},

    // 用户与权益
    lightPoints: 0,
    freeMode: false,
    rightsOk: true,

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
    this.setData({ lightPoints: app.globalData.lightPoints });
  },

  loadUserData() {
    this.setData({
      lightPoints: app.globalData.lightPoints,
      freeMode: app.globalData.freeMode
    });

    api.config().then((c) => {
      if (!c) return;
      const free = !!c.free_mode;
      app.globalData.freeMode = free;
      const prices = c.prices || {};
      const pLight = (prices.light != null) ? prices.light : this.data.costLight;
      const pFine = (prices.fine != null) ? prices.fine : this.data.costFine;

      this.setData({
        freeMode: free,
        costLight: pLight,
        costFine: pFine,
        priceRangeText: `${pLight}~${pFine} 光子`
      });
      this.refreshCurrentCost();
    }).catch(() => {});

    // 光子余额以服务端为准
    api.me().then((d) => {
      if (d && typeof d.balance === 'number') app.setBalance(d.balance);
      this.setData({ lightPoints: app.globalData.lightPoints });
    }).catch(() => {});
  },

  loadTemplatesData(selectedTid) {
    api.templates().then((d) => {
      const items = (d && d.items) || [];
      const list = items.map((t) => Object.assign({}, t, {
        coverUrl: t.cover ? api.absolute(t.cover) : '/images/logo.jpg'
      }));

      // 首位增加【原片修复】基础选项（画质超分与去噪，不改变面部特征与风格）
      const restoreOption = {
        id: '',
        name: '原片修复',
        subtitle: '原片画质修复增强，不改变风格与面貌',
        coverUrl: '/images/logo.jpg'
      };
      const fullList = [restoreOption].concat(list);

      let current = selectedTid ? list.find((t) => t.id === selectedTid) : null;

      this.setData({
        allTemplates: fullList,
        selectedTemplate: current || null,
        templateId: current ? current.id : ''
      });
      if (current) this.applyTemplateTextDefaults(current);
      this.refreshCurrentCost();
    }).catch(() => {});
  },

  /** 模板文字字段默认值（{today} 换成当天） */
  applyTemplateTextDefaults(tpl) {
    const textValues = {};
    (tpl.text_fields || []).forEach((f) => {
      let v = f.default || '';
      if (v === '{today}') {
        const d = new Date();
        const p = (n) => (n < 10 ? '0' + n : '' + n);
        v = d.getFullYear() + '.' + p(d.getMonth() + 1) + '.' + p(d.getDate());
      }
      textValues[f.key] = v;
    });
    this.setData({ textValues: textValues });
  },

  /** 当前消耗光子：模板价 > 0 用模板价，否则按（模板/手选）档位的默认价 */
  refreshCurrentCost() {
    const tpl = this.data.selectedTemplate;
    let cost;
    if (tpl) {
      const tierPrice = tpl.engine === 'fine' ? this.data.costFine : this.data.costLight;
      cost = (typeof tpl.price === 'number' && tpl.price > 0) ? tpl.price : tierPrice;
    } else {
      cost = this.data.quality === 'fine' ? this.data.costFine : this.data.costLight;
    }
    this.setData({ currentQualityCost: cost });
  },

  /** 模板文字排版字段输入 */
  onTextFieldInput(e) {
    const key = e.currentTarget.dataset.key;
    this.setData({ ['textValues.' + key]: e.detail.value });
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

  /** 切换风格（选了模板时档位跟模板走，选【原片修复】进入纯修复自由档位） */
  onSelectTemplate(e) {
    const tpl = e.currentTarget.dataset.template;
    if (!tpl) return;
    const targetId = tpl.id || '';
    if (targetId === this.data.templateId) return;

    if (!targetId) {
      // 切换回【原片修复】基础模式
      this.setData({
        selectedTemplate: null,
        templateId: '',
        textValues: {}
      });
      this.refreshCurrentCost();
      try { wx.vibrateShort({ type: 'light' }); } catch (err) {}
      return;
    }

    this.setData({
      selectedTemplate: tpl,
      templateId: tpl.id
    });
    this.applyTemplateTextDefaults(tpl);
    this.refreshCurrentCost();
    try {
      wx.vibrateShort({ type: 'light' });
    } catch (err) {}
  },

  /** 纯修复模式切换生成档位（选了模板时档位跟模板走，此入口隐藏） */
  onSelectQuality(e) {
    const q = e.currentTarget.dataset.quality;
    this.setData({ quality: q });
    this.refreshCurrentCost();
    try {
      wx.vibrateShort({ type: 'light' });
    } catch (err) {}
  },

  /** 切换版权勾选 */
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

  /** 点击【开始生成】 */
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
    if (!this.data.freeMode && this.data.lightPoints < cost) {
      wx.showModal({
        title: '光子余额不足',
        content: `本次生成需要 ✦${cost}，当前余额 ✦${this.data.lightPoints}。是否前往补给？`,
        confirmText: '补充光子',
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
    const tpl = this.data.selectedTemplate;
    const ratio = this.data.currentRatioKey;

    try {
      const formData = {
        quality: this.data.quality,
        aspect_ratio: ratio
      };
      if (tpl && tpl.id) {
        formData.template_id = tpl.id;
        // 模板文字排版字段：后端做长度截断与默认值补齐
        if ((tpl.text_fields || []).length) {
          formData.text_fields = JSON.stringify(this.data.textValues);
        }
      }

      // COS 直传优先（图片字节不过服务器），失败自动回退 multipart
      const created = await api.submitJob(path, formData);
      if (!created || created.code !== 0 || !created.job_id) {
        throw new Error((created && created.detail) || '服务响应异常');
      }
      if (typeof created.balance === 'number') app.setBalance(created.balance);

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

      const origUrl = api.absolute(job.orig_url || created.orig_url);
      const resUrl = api.absolute(job.result_url || created.result_url);
      const bustUrl = resUrl + (resUrl.indexOf('?') >= 0 ? '&' : '?') + 't=' + Date.now();

      // 记录历史（存 jobId，展示时可向服务端换取新鲜签名直链）
      const historyItem = {
        original: origUrl,
        result: bustUrl,
        quality: created.quality || this.data.quality,
        templateName: tpl ? tpl.name : '',
        jobId: created.job_id,
        time: this.formatTime(new Date())
      };
      app.globalData.historyList = app.globalData.historyList || [];
      app.globalData.historyList.unshift(historyItem);
      if (app.globalData.historyList.length > 50) app.globalData.historyList.length = 50;
      app.persist();

      this.setData({ processing: false, lightPoints: app.globalData.lightPoints });

      // 跳转至全屏拖拽滑块对比页（带 jobId，供签名过期后刷新）
      wx.redirectTo({
        url: `/pages/compare/compare?original=${encodeURIComponent(origUrl)}&result=${encodeURIComponent(bustUrl)}&quality=${historyItem.quality}&job=${encodeURIComponent(created.job_id)}`
      });
    } catch (err) {
      console.error('生成失败', err);
      this.setData({ processing: false });
      this.setData({ lightPoints: app.globalData.lightPoints });

      // 402 = 服务端判定光子不足（余额是服务端记账，客户端预判可能过期）
      if (err && err.status === 402) {
        wx.showModal({
          title: '光子余额不足',
          content: err.message || '光子不足，前往补给？',
          confirmText: '补充光子',
          cancelText: '取消',
          success: (r) => {
            if (r.confirm) wx.navigateTo({ url: '/pages/credits/credits' });
          }
        });
        return;
      }
      // 内容安全审核拦截提示
      if (err && err.message && (err.message.includes('安全审核') || err.message.includes('审核'))) {
        wx.showModal({
          title: '内容安全提示',
          content: err.message,
          showCancel: false,
          confirmText: '我知道了'
        });
        return;
      }

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
