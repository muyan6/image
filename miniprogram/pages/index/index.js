const app = getApp();
const api = require('../../utils/api.js');

Page({
  data: {
    lightPoints: 0,
    freeMode: false,
    priceLight: 1,
    priceFine: 3,
    notice: null,
    featuredTemplates: [
      { id: "t_anime_dots", name: "日漫错彩网点", subtitle: "复古日漫彩页肖像，荧光波普风格", engine: "fine", price: 0, coverUrl: "/images/logo.jpg" },
      { id: "t_clarity", name: "冷白通透", subtitle: "去黄除暗，高光清爽，冷白皮质感", engine: "light", price: 0, coverUrl: "/images/logo.jpg" },
      { id: "t_felt", name: "毛毡旅行档案", subtitle: "定格动画毛毡质感，复古手作肌理", engine: "fine", price: 0, coverUrl: "/images/logo.jpg" },
      { id: "t_fuji", name: "富士胶片", subtitle: "微暖复古，温润通透，街拍人像百搭", engine: "light", price: 0, coverUrl: "/images/logo.jpg" },
      { id: "t_poster", name: "复古电影海报", subtitle: "戏剧光影底图 + 中文标题排版", engine: "fine", price: 0, coverUrl: "/images/logo.jpg" }
    ],
    activeTemplate: null,
    historyCount: 0
  },

  onLoad(options) {
    // 分享路径携带的邀请码：先存本地，登录成功后绑定（服务端发奖，仅一次）
    if (options && options.invite) {
      try { wx.setStorageSync('pendingInvite', options.invite); } catch (e) {}
    }
    this.loadNotice();
    this.loadTemplates();
    this.bindPendingInvite();
  },

  onShow() {
    this.loadConfig();
    this.setData({
      lightPoints: app.globalData.lightPoints,
      freeMode: app.globalData.freeMode,
      historyCount: (app.globalData.historyList || []).length
    });

    // 检查是否有跨页面携带过来的模板（模板库/社区点了「做同款」）
    if (app.globalData.selectedTemplate) {
      const tpl = app.globalData.selectedTemplate;
      app.globalData.selectedTemplate = null;
      this.setData({ activeTemplate: tpl });
    }
  },

  /** 登录后绑定邀请码，双方 +30 光子（失败静默，不打扰正常使用） */
  bindPendingInvite() {
    let code = '';
    try { code = wx.getStorageSync('pendingInvite') || ''; } catch (e) {}
    if (!code) return;
    api.ensureLogin()
      .then(() => api.bindInvite(code))
      .then((d) => {
        try { wx.removeStorageSync('pendingInvite'); } catch (e) {}
        if (d && typeof d.balance === 'number') app.setBalance(d.balance);
        this.setData({ lightPoints: app.globalData.lightPoints });
        wx.showToast({ title: '邀请奖励 ✦30 已到账', icon: 'none' });
      })
      .catch((err) => {
        try { wx.removeStorageSync('pendingInvite'); } catch (e) {}
        console.warn('邀请码绑定失败', err);
      });
  },

  loadTemplates() {
    // 1. 尝试读本地缓存，保证秒开
    try {
      const cached = wx.getStorageSync('cached_templates_items');
      if (Array.isArray(cached) && cached.length > 0) {
        this._lastTemplates = cached;
        this._renderFeatured(cached);
      }
    } catch (e) {}

    // 2. 异步拉取服务端最新模板
    api.templates()
      .then((d) => {
        const items = (d && d.items) || [];
        this._lastTemplates = items;
        try { wx.setStorageSync('cached_templates_items', items); } catch (e) {}
        this._renderFeatured(items);
      })
      .catch((err) => {
        console.warn('获取服务端模板失败，使用预设模板', err);
      });
  },

  _renderFeatured(items) {
    const free = !!this.data.freeMode;
    const pFine = this.data.priceFine || 3;
    const pLight = this.data.priceLight || 1;
    const featured = items.slice(0, 6).map((t) => {
      let cost = free ? '免扣费' : (t.price > 0 ? ('✦ ' + t.price + ' 光子') : (t.engine === 'fine' ? ('✦ ' + pFine + ' 光子') : ('✦ ' + pLight + ' 光子')));
      return Object.assign({}, t, {
        coverUrl: t.cover ? api.absolute(t.cover) : '/images/logo.jpg',
        costText: cost
      });
    });
    this.setData({ featuredTemplates: featured });
  },

  onGoToTemplates() {
    wx.switchTab({
      url: '/pages/templates/templates'
    });
  },

  onSelectFeaturedTemplate(e) {
    const tpl = e.currentTarget.dataset.template;
    if (!tpl) return;
    wx.navigateTo({
      url: `/pages/style-detail/style-detail?id=${encodeURIComponent(tpl.id)}`
    });
  },

  /** 清除待使用模板 */
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
        app.globalData.freeMode = !!c.free_mode;
        this.setData({
          priceLight: (p.light != null) ? p.light : this.data.priceLight,
          priceFine: (p.fine != null) ? p.fine : this.data.priceFine,
          freeMode: !!c.free_mode
        });
        if (this._lastTemplates) this._renderFeatured(this._lastTemplates);
      })
      .catch(() => {});
    // 光子余额以服务端为准
    api.me()
      .then((d) => {
        if (d && typeof d.balance === 'number') app.setBalance(d.balance);
        this.setData({ lightPoints: app.globalData.lightPoints });
      })
      .catch(() => {});
  },

  /** 选择照片入口：选完直接跳转至【调整作品】页，待用模板一并带过去 */
  onPickImage() {
    wx.chooseMedia({
      count: 1,
      mediaType: ['image'],
      sourceType: ['album', 'camera'],
      sizeType: ['original', 'compressed'],
      success: (res) => {
        const file = res.tempFiles[0];
        if (!file || !file.tempFilePath) return;
        const pending = this.data.activeTemplate;
        const defaultTid = pending ? pending.id : '';
        if (pending) this.setData({ activeTemplate: null });

        wx.navigateTo({
          url: `/pages/adjust/adjust?image=${encodeURIComponent(file.tempFilePath)}&templateId=${encodeURIComponent(defaultTid)}`
        });
      }
    });
  },

  /** 积分中心页面 */
  onOpenCreditModal() {
    wx.navigateTo({
      url: '/pages/credits/credits'
    });
  },

  /** 我的作品管理页面 */
  onOpenHistory() {
    wx.navigateTo({
      url: '/pages/works/works'
    });
  }
});
