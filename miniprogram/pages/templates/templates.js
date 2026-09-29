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

    api.templates()
      .then((data) => {
        const groups = (data && data.groups) || [];
        const rawItems = (data && data.items) || [];

        const items = rawItems.map((item) => {
          return Object.assign({}, item, {
            coverUrl: item.cover ? api.absolute(item.cover) : '/images/logo.jpg'
          });
        });

        // 统计各分类计数
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
          categories: cats,
          loading: false
        });

        this.filterByCategory(this.data.activeCategory);
        if (typeof callback === 'function') callback();
      })
      .catch((err) => {
        console.warn('获取模板列表失败', err);
        this.setData({ loading: false });
        if (typeof callback === 'function') callback();
      });
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
