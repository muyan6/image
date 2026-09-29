const app = getApp();
const api = require('../../utils/api.js');

Page({
  data: {
    loading: false,
    categories: [
      { id: 'all', name: '全部风格', count: 0 }
    ],
    activeCategory: 'all',
    allTemplates: [],
    filteredTemplates: [],
    selectedItem: null
  },

  onLoad() {
    this.fetchTemplates();
  },

  onPullDownRefresh() {
    this.fetchTemplates(() => {
      wx.stopPullDownRefresh();
    });
  },

  fetchTemplates(callback) {
    this.setData({ loading: true });

    // 1. 尝试读本地缓存，秒开
    try {
      const cached = wx.getStorageSync('cached_templates_data');
      if (cached && Array.isArray(cached.items) && cached.items.length > 0) {
        this._renderData(cached.groups || [], cached.items);
      }
    } catch (e) {}

    // 2. 异步请求后端
    api.templates()
      .then((data) => {
        const groups = (data && data.groups) || [];
        const rawItems = (data && data.items) || [];
        if (rawItems.length > 0) {
          try { wx.setStorageSync('cached_templates_data', data); } catch (e) {}
          this._renderData(groups, rawItems);
        }
        this.setData({ loading: false });
        if (typeof callback === 'function') callback();
      })
      .catch((err) => {
        console.warn('获取模板列表失败', err);
        this.setData({ loading: false });
        if (typeof callback === 'function') callback();
      });
  },

  _renderData(groups, rawItems) {
    const items = rawItems.map((item) => {
      const cost = item.price > 0 ? ('✦ ' + item.price + ' 光子') : (item.engine === 'fine' ? '✦ 3 光子' : '✦ 1 光子');
      return Object.assign({}, item, {
        coverUrl: item.cover ? api.absolute(item.cover) : '/images/logo.jpg',
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

    this.setData({
      allTemplates: items,
      categories: cats
    });

    this.filterByCategory(this.data.activeCategory);
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
  }
});
