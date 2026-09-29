const app = getApp();

Page({
  data: {
    fishTokens: 90,
    checkedIn: true,
    records: [
      { id: 'r1', title: '每日签到奖励', time: '今日 08:52', amount: 10 },
      { id: 'r2', title: '首次登录赠送新人算力', time: '昨天 22:29', amount: 80 }
    ]
  },

  onLoad() {
    this.refreshData();
  },

  onShow() {
    this.refreshData();
  },

  refreshData() {
    this.setData({
      fishTokens: app.globalData.fishTokens || 0
    });
  },

  onCheckIn() {
    if (this.data.checkedIn) {
      wx.showToast({ title: '今日已完成签到', icon: 'none' });
      return;
    }
    app.addToken(10);
    this.setData({
      checkedIn: true,
      fishTokens: app.globalData.fishTokens
    });
    wx.showToast({ title: '签到成功 +10 积分', icon: 'success' });
  },

  onSelectPackage(e) {
    const name = e.currentTarget.dataset.name;
    const price = e.currentTarget.dataset.price;
    wx.showModal({
      title: '积分补给',
      content: `确认获取【${name}】（金额 ¥${price}）吗？\n当前为演示沙盒环境，确认后将直接为您注入积分。`,
      confirmText: '立即充值',
      confirmColor: '#1a1917',
      cancelText: '取消',
      success: (res) => {
        if (res.confirm) {
          const add = parseInt(name, 10) || 600;
          app.addToken(add);
          this.refreshData();
          wx.showToast({ title: `已成功注入 +${add} 积分`, icon: 'success' });
        }
      }
    });
  },

  onGoInvite() {
    wx.navigateTo({
      url: '/pages/invite/invite'
    });
  }
});
