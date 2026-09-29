const app = getApp();
const api = require('../../utils/api.js');

Page({
  data: {
    works: []
  },

  onLoad() {
    this.loadWorks();
  },

  onShow() {
    this.loadWorks();
  },

  onPullDownRefresh() {
    this.loadWorks();
    wx.stopPullDownRefresh();
  },

  loadWorks() {
    this.setData({
      works: app.globalData.historyList || []
    });
  },

  /**
   * 刷新单件作品的直链：历史里存的是 2 小时时效的 COS 签名 URL，
   * 展示前向服务端用 jobId 换一张新鲜的（拿不到就维持原样）。
   */
  refreshWork(item) {
    if (!item || !item.jobId) return Promise.resolve(item);
    return api.request('/api/jobs/' + item.jobId)
      .then((job) => {
        if (!job || job.status !== 'succeeded' || !job.result_url) return item;
        const fresh = Object.assign({}, item, {
          original: api.absolute(job.orig_url || item.original),
          result: api.absolute(job.result_url)
        });
        const list = app.globalData.historyList || [];
        const idx = list.findIndex((w) => w.jobId === item.jobId);
        if (idx >= 0) {
          list[idx] = fresh;
          app.persist();
          this.loadWorks();
        }
        return fresh;
      })
      .catch(() => item);
  },

  onTapWork(e) {
    const index = e.currentTarget.dataset.index;
    const item = this.data.works[index];
    if (!item) return;

    this.refreshWork(item).then((fresh) => {
      wx.showActionSheet({
        itemList: ['全屏高清查看并保存', '对比原图模式', '删除此件作品'],
        itemColor: '#1a1917',
        success: (res) => {
          if (res.tapIndex === 0) {
            wx.previewImage({
              current: fresh.result,
              urls: [fresh.result, fresh.original].filter(Boolean)
            });
          } else if (res.tapIndex === 1) {
            wx.navigateTo({
              url: `/pages/compare/compare?result=${encodeURIComponent(fresh.result)}&original=${encodeURIComponent(fresh.original || '')}&job=${encodeURIComponent(fresh.jobId || '')}`
            });
          } else if (res.tapIndex === 2) {
            this.deleteSingleWork(index);
          }
        }
      });
    });
  },

  deleteSingleWork(index) {
    wx.showModal({
      title: '删除作品',
      content: '确定要从典藏列表中移除这件作品吗？',
      confirmText: '删除',
      confirmColor: '#9e4b3c',
      cancelText: '保留',
      success: (res) => {
        if (res.confirm) {
          app.globalData.historyList.splice(index, 1);
          app.persist();
          this.loadWorks();
          wx.showToast({ title: '已移除作品', icon: 'success' });
        }
      }
    });
  },

  onClearAllWorks() {
    wx.showModal({
      title: '清空作品集',
      content: '确定要清空全部已留存的作品吗？操作后不可恢复。',
      confirmText: '清空全部',
      confirmColor: '#9e4b3c',
      cancelText: '保留',
      success: (res) => {
        if (res.confirm) {
          app.clearHistory();
          this.loadWorks();
          wx.showToast({ title: '作品集已清空', icon: 'success' });
        }
      }
    });
  },

  onGoCreate() {
    wx.switchTab({
      url: '/pages/index/index'
    });
  }
});
