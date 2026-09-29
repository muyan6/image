const app = getApp();

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

  onTapWork(e) {
    const index = e.currentTarget.dataset.index;
    const item = this.data.works[index];
    if (!item) return;

    wx.showActionSheet({
      itemList: ['全屏高清查看并保存', '对比原图模式', '删除此件作品'],
      itemColor: '#1a1917',
      success: (res) => {
        if (res.tapIndex === 0) {
          wx.previewImage({
            current: item.result,
            urls: [item.result, item.original].filter(Boolean)
          });
        } else if (res.tapIndex === 1) {
          wx.navigateTo({
            url: `/pages/compare/compare?result=${encodeURIComponent(item.result)}&original=${encodeURIComponent(item.original || '')}`
          });
        } else if (res.tapIndex === 2) {
          this.deleteSingleWork(index);
        }
      }
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
