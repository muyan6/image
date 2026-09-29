const app = getApp();
const api = require('../../utils/api.js');

Page({
  data: {
    imagePath: '',
    templateId: '',
    selectedTemplate: null,
    allTemplates: [],

    // 画幅设置：默认保持原图画幅与比例，绝不强制裁剪
    currentRatioKey: 'original',
    currentRatioLabel: '保持原图',
    previewStyle: 'height: 694rpx;',
    showRatioModal: false,
    ratioOptions: [
      { key: 'original', label: '保持原图', desc: '保留原始拍摄长宽比例，不进行裁剪', rec: true },
      { key: '1:1', label: '1:1', desc: '经典方形构图，社交分享与人像首选', rec: false },
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
    processingText: '正在提交照片…',
    currentJobId: null
  },

  onLoad(options) {
    const img = options.image ? decodeURIComponent(options.image) : '';
    const tid = options.templateId ? decodeURIComponent(options.templateId) : '';

    this.setData({
      imagePath: img,
      templateId: tid
    });

    if (img) {
      wx.getImageInfo({
        src: img,
        success: (info) => {
          if (info && info.width > 0 && info.height > 0) {
            this._imgWidth = info.width;
            this._imgHeight = info.height;
            if (this.data.currentRatioKey === 'original') {
              const stageH = Math.min(1050, Math.max(390, Math.round(694 * (info.height / info.width))));
              this.setData({ previewStyle: `height: ${stageH}rpx;` });
            }
          }
        }
      });
    }

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
    };
    let style = heightMap[key];
    if (key === 'original') {
      if (this._imgWidth && this._imgHeight) {
        const stageH = Math.min(1050, Math.max(390, Math.round(694 * (this._imgHeight / this._imgWidth))));
        style = `height: ${stageH}rpx;`;
      } else {
        style = 'height: 694rpx;';
      }
    }
    this.setData({
      currentRatioKey: key,
      currentRatioLabel: label,
      previewStyle: style || 'height: 694rpx;',
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
    // 小于 6MB 的照片直接无损上传，保留全部高频细节；超大原图才做轻微压缩
    wx.getFileInfo({
      filePath: filePath,
      success: (finfo) => {
        if (finfo && finfo.size && finfo.size > 6 * 1024 * 1024) {
          wx.compressImage({
            src: filePath,
            quality: 92,
            success: (res) => this.executeUpload(res.tempFilePath || filePath),
            fail: () => this.executeUpload(filePath)
          });
        } else {
          this.executeUpload(filePath);
        }
      },
      fail: () => this.executeUpload(filePath)
    });
  },

  onCancelOrMinimizeWait() {
    this.setData({ processing: false });
    wx.showToast({
      title: '已转入后台，完成后自动存入作品',
      icon: 'none',
      duration: 2500
    });
    setTimeout(() => {
      wx.navigateTo({ url: '/pages/works/works' });
    }, 600);
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

      this.setData({
        processing: true,
        processingText: '正在提交照片并云端排队…',
        currentJobId: null
      });

      // 1. COS 直传优先，失败自动回退 multipart（耗时仅 1~2 秒）
      const created = await api.submitJob(path, formData);
      if (!created || created.code !== 0 || !created.job_id) {
        throw new Error((created && created.detail) || '服务响应异常');
      }
      if (typeof created.balance === 'number') app.setBalance(created.balance);

      const jobId = created.job_id;
      const origUrl = api.absolute(created.orig_url || path);

      // 【关键优化】：上传成功后立即写入作品集历史！
      // 哪怕用户此刻立即切屏、关闭小程序或断网，作品都绝对不会丢失！
      const initialItem = {
        original: origUrl,
        result: '',
        status: 'processing',
        quality: created.quality || this.data.quality,
        templateName: tpl ? tpl.name : '',
        jobId: jobId,
        time: this.formatTime(new Date())
      };
      app.globalData.historyList = app.globalData.historyList || [];
      const exIdx = app.globalData.historyList.findIndex((h) => h.jobId === jobId);
      if (exIdx >= 0) {
        app.globalData.historyList[exIdx] = initialItem;
      } else {
        app.globalData.historyList.unshift(initialItem);
      }
      if (app.globalData.historyList.length > 50) app.globalData.historyList.length = 50;
      app.persist();

      this.setData({
        currentJobId: jobId,
        processingText: `正在进行${tpl ? tpl.name : 'AI'}风格重构…`
      });

      // 2. 轮询等待任务（用户若停留在本页等待则轮询；随时可关闭或切屏）
      let job = null;
      try {
        job = await api.waitForJob(jobId, {
          isCanceled: () => !this.data.processing,
          onTick: (j) => {
            if (j.stage === 'enhance') {
              this.setData({ processingText: 'AI 深度重构光影与细节中…' });
            }
          }
        });
      } catch (waitErr) {
        const msg = String((waitErr && waitErr.message) || '');
        if (waitErr.code === 'USER_BACKGROUND' || msg.includes('canceled') || msg.includes('abort') || !this.data.processing) {
          console.log('切屏或后台等待，任务已在云端继续运行:', jobId);
          this.setData({ processing: false });
          return;
        }
        throw waitErr;
      }

      if (!job || job.status !== 'succeeded') {
        throw new Error((job && job.error) || '生成未完成，请在作品中查看');
      }

      // 注意：COS 预签名 URL 包含 HMAC 校验，绝不可外挂拼接 &t=Date.now()，否则直接报 403 SignatureDoesNotMatch
      const resUrl = api.absolute(job.result_url || created.result_url);

      // 提取最新的原图直链（服务端已同步画幅几何对齐）
      const finalOrigUrl = api.absolute(job.orig_url || created.orig_url || origUrl);
      const compareOrig = finalOrigUrl || path;

      // 更新历史记录为完成状态
      const finishedItem = {
        original: finalOrigUrl,
        result: resUrl,
        status: 'succeeded',
        quality: created.quality || this.data.quality,
        templateName: tpl ? tpl.name : '',
        jobId: jobId,
        time: this.formatTime(new Date())
      };
      const list = app.globalData.historyList || [];
      const idx = list.findIndex((h) => h.jobId === jobId);
      if (idx >= 0) list[idx] = finishedItem;
      app.persist();

      this.setData({ processing: false, lightPoints: app.globalData.lightPoints });

      // 跳转至全屏拖拽滑块对比页
      wx.redirectTo({
        url: `/pages/compare/compare?original=${encodeURIComponent(compareOrig)}&result=${encodeURIComponent(resUrl)}&quality=${finishedItem.quality}&job=${encodeURIComponent(jobId)}`
      });

    } catch (err) {
      console.error('生成失败', err);
      this.setData({ processing: false });
      this.setData({ lightPoints: app.globalData.lightPoints });

      const msg = String((err && err.message) || '');
      // 切屏取消类错误静默忽略（任务已在作品库中）
      if (msg.includes('canceled') || msg.includes('abort')) {
        return;
      }

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
