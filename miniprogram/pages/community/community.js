const app = getApp();
const api = require('../../utils/api.js');

/**
 * 灵感沙龙（社区）
 *
 * 帖子来自 GET /api/community，在 /admin「社区管理」中逐帖维护，
 * 前端不再内置任何示例/测试文案（此前是 4 条写死的假展品）。
 * 暂停展示和已删除的帖子不会返回；置顶与排序由服务端决定。
 */
Page({
  data: {
    filters: [
      { id: 'all', name: '全部展品' },
      { id: 'portrait', name: '冷白人像' },
      { id: 'film', name: '复古胶片' },
      { id: 'old_photo', name: '老照片复苏' },
      { id: 'anime', name: '动漫重绘' }
    ],
    activeFilter: 'all',
    items: [],
    filteredItems: [],
    enabled: false,
    loading: true
  },

  onLoad() {
    this.loadCommunity();
  },

  onShow() {
    if (this._communityLoaded) this.loadCommunity();
  },

  onPullDownRefresh() {
    this.loadCommunity().then(() => wx.stopPullDownRefresh());
  },

  /** 拉取沙龙展品：后端未开启或无内容时返回空列表，不做任何本地兜底 */
  loadCommunity() {
    return api.request('/api/community', { timeout: 8000 })
      .then((d) => {
        this._communityLoaded = true;
        const items = ((d && d.items) || []).map(item => Object.assign({}, item, {
          resultUrl: api.absolute(item.resultUrl), origUrl: api.absolute(item.origUrl),
          authorAvatar: item.authorAvatar ? api.absolute(item.authorAvatar) : ''
        }));
        this.setData({
          enabled: !!(d && d.enabled),
          items: items,
          loading: false
        });
        this.filterItems(this.data.activeFilter);
      })
      .catch(() => {
        // 网络异常按「暂无内容」处理，不展示假数据
        this.setData({
          enabled: false,
          items: [],
          filteredItems: [],
          loading: false
        });
      });
  },

  onSelectFilter(e) {
    const fid = e.currentTarget.dataset.id;
    if (fid === this.data.activeFilter) return;
    this.setData({ activeFilter: fid });
    this.filterItems(fid);
  },

  filterItems(fid) {
    const all = this.data.items;
    if (fid === 'all') {
      this.setData({ filteredItems: all });
    } else {
      this.setData({
        filteredItems: all.filter((x) => x.category === fid)
      });
    }
  },

  onLikeItem(e) {
    const id = e.currentTarget.dataset.id;
    const bump = (item) => {
      if (item.id !== id) return item;
      const nextLiked = !item.liked;
      return Object.assign({}, item, {
        liked: nextLiked,
        likes: nextLiked ? item.likes + 1 : item.likes - 1
      });
    };
    this.setData({
      items: this.data.items.map(bump),
      filteredItems: this.data.filteredItems.map(bump)
    });
    wx.showToast({
      title: '感谢赞叹',
      icon: 'none'
    });
  },

  onPreviewExhibit(e) {
    const item = e.currentTarget.dataset.item;
    const type = e.currentTarget.dataset.type;
    const urls = [item.resultUrl, item.origUrl].filter(Boolean);
    if (!urls.length) return;
    const current = type === 'original' && item.origUrl
      ? item.origUrl
      : (item.resultUrl || urls[0]);
    wx.previewImage({
      current: current,
      urls: urls
    });
  },

  onShareExhibit(e) {
    const title = e.currentTarget.dataset.title;
    wx.showShareImageMenu ? wx.showShareImageMenu({ path: '/images/logo.jpg' }) : wx.showToast({
      title: '请点击右上角【···】分享',
      icon: 'none'
    });
  },

  onMakeSame(e) {
    const tid = e.currentTarget.dataset.templateId;
    const tname = e.currentTarget.dataset.name;
    if (!tid) {
      wx.showToast({ title: '该展品未绑定配方', icon: 'none' });
      return;
    }

    // 只带 id/名字；档位与价格由调整页从服务端模板数据现读，不在这里猜
    app.globalData.selectedTemplate = {
      id: tid,
      name: tname
    };

    wx.showToast({
      title: `已选用配方：${tname}`,
      icon: 'none'
    });

    setTimeout(() => {
      wx.switchTab({
        url: '/pages/index/index'
      });
    }, 400);
  }
});
