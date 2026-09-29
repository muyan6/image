const app = getApp();
const api = require('../../utils/api.js');

Page({
  data: {
    loading: false,
    categories: [
      { id: 'all', name: '全部风格', count: 0 }
    ],
    activeCategory: 'all',
    freeMode: false,
    priceLight: 1,
    priceFine: 3,
    allTemplates: [],
    filteredTemplates: [],
    selectedItem: null
  },

  onLoad() {
    // tabBar 页面每次回到前台都需刷新，不在 onLoad 再重复请求一次。
  },

  onShow() {
    this.fetchTemplates();
  },

  onPullDownRefresh() {
    this.fetchTemplates(() => {
      wx.stopPullDownRefresh();
    });
  },

  fetchTemplates(callback) {
    const version = (this._fetchVersion || 0) + 1;
    this._fetchVersion = version;
    this.setData({ loading: true });
    if (this.data.freeMode !== !!app.globalData.freeMode && !this._latestRawItems) {
      this.setData({ freeMode: !!app.globalData.freeMode });
    }

    // 1. 尝试读本地缓存，秒开
    try {
      const cached = wx.getStorageSync('cached_templates_data');
      if (cached && Array.isArray(cached.items) && cached.items.length > 0) {
        this._renderData(cached.groups || [], cached.items);
      }
    } catch (e) {}

    // 2. 异步请求后端
    // 模板目录与价格分开获取；任意一方先回来都用最新两份数据重新渲染。
    api.config().then((config) => {
      if (version !== this._fetchVersion || !config) return;
      const prices = config.prices || {};
      app.globalData.freeMode = !!config.free_mode;
      this.setData({ freeMode: !!config.free_mode,
        priceLight: prices.light != null ? prices.light : this.data.priceLight,
        priceFine: prices.fine != null ? prices.fine : this.data.priceFine });
      if (this._latestRawItems) this._renderData(this._latestGroups, this._latestRawItems);
    }).catch(() => {});

    api.templates()
      .then((data) => {
        if (version !== this._fetchVersion) {
          if (typeof callback === 'function') callback();
          return;
        }
        const groups = (data && data.groups) || [];
        const rawItems = (data && data.items) || [];
        try { wx.setStorageSync('cached_templates_data', data); } catch (e) {}
        this._renderData(groups, rawItems);
        this.setData({ loading: false });
        if (typeof callback === 'function') callback();
      })
      .catch((err) => {
        if (version !== this._fetchVersion) {
          if (typeof callback === 'function') callback();
          return;
        }
        console.warn('获取模板列表失败', err);
        this.setData({ loading: false });
        if (typeof callback === 'function') callback();
      });
  },

  _renderData(groups, rawItems) {
    this._latestGroups = groups;
    this._latestRawItems = rawItems;
    const items = rawItems.map((item) => {
      const amount = item.price > 0 ? item.price :
        (item.engine === 'fine' ? this.data.priceFine : this.data.priceLight);
      const cost = this.data.freeMode ? '免扣费' : ('✦ ' + amount + ' 光子');
      const rawCovers = Array.isArray(item.covers) && item.covers.length > 0
        ? item.covers
        : (item.cover ? [item.cover] : []);
      const coverUrls = rawCovers.map((c) => api.absolute(c)).filter(Boolean);
      return Object.assign({}, item, {
        covers: coverUrls,
        coverUrl: coverUrls[0] || (item.cover ? api.absolute(item.cover) : '/images/logo.jpg'),
        costText: cost
      });
    });

    const cats = [
      { id: 'all', name: '全部风格', count: items.length }
    ];

    groups.forEach((g) => {
      const count = items.filter((x) => x.group_id === g.id).length;
      cats.push({
        id: g.id,
        name: g.name,
        count: count
      });
    });

    const category = cats.some((c) => c.id === this.data.activeCategory) ? this.data.activeCategory : 'all';
    this.setData({
      allTemplates: items,
      categories: cats,
      activeCategory: category
    });

    this.filterByCategory(category);
  },

  onSelectCategory(e) {
    const cid = e.currentTarget.dataset.id;
    if (cid === this.data.activeCategory) return;
    this.setData({ activeCategory: cid });
    this.filterByCategory(cid);
  },

  filterByCategory(cid) {
    const list = this.data.allTemplates;
    if (cid === 'all') {
      this.setData({ filteredTemplates: list });
    } else {
      const filtered = list.filter((x) => x.group_id === cid);
      this.setData({ filteredTemplates: filtered });
    }
  },

  onOpenDetail(e) {
    const item = e.currentTarget.dataset.template;
    if (!item) return;
    wx.navigateTo({
      url: `/pages/style-detail/style-detail?id=${encodeURIComponent(item.id)}`
    });
  },

  onCloseDetail() {
    this.setData({ selectedItem: null });
  },

  onApplyTemplate() {
    const tpl = this.data.selectedItem;
    if (!tpl) return;
    this.setData({ selectedItem: null });
    wx.navigateTo({
      url: `/pages/style-detail/style-detail?id=${encodeURIComponent(tpl.id)}`
    });
  },

  onPreviewCovers(e) {
    const current = e.currentTarget.dataset.url;
    const covers = (this.data.selectedItem && this.data.selectedItem.covers) || [];
    if (!covers.length) return;
    wx.previewImage({
      current: current || covers[0],
      urls: covers
    });
  }
});
