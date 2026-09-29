const app = getApp();
const api = require('../../utils/api.js');

Page({
  data: {
    lightPoints: 0,
    freeMode: false,
    checkedIn: false,
    records: [
      { id: 'r1', title: '每日签到奖励', time: '今日', amount: 10 },
      { id: 'r2', title: '新人光子礼包', time: '注册时', amount: 90 }
    ]
  },

  onLoad() {
    this.refreshData();
  },

  onShow() {
    this.refreshData();
  },

  refreshData() {
    this.setData({ lightPoints: app.globalData.lightPoints });

    api.config().then((c) => {
      if (c) this.setData({ freeMode: !!c.free_mode });
    }).catch(() => {});

    // 签到状态与余额都以服务端为准
    api.me().then((d) => {
      if (!d) return;
      if (typeof d.balance === 'number') app.setBalance(d.balance);
      this.setData({
        lightPoints: app.globalData.lightPoints,
        checkedIn: !!(d.earn && d.earn.checkin_done)
      });
    }).catch(() => {});
  },

  /** 每日签到：服务端记账（1 次/天），成功 +10 光子 */
  onCheckIn() {
    if (this.data.checkedIn) {
      wx.showToast({ title: '今日已签到', icon: 'none' });
      return;
    }
    api.earn('checkin')
      .then((d) => {
        if (d && typeof d.balance === 'number') app.setBalance(d.balance);
        this.setData({
          checkedIn: true,
          lightPoints: app.globalData.lightPoints
        });
        wx.showToast({ title: '签到成功 ✦10 光子', icon: 'success' });
      })
      .catch((err) => {
        wx.showToast({ title: err.message || '签到失败，稍后再试', icon: 'none' });
      });
  },

  /** 充值套餐：微信支付尚未接入，如实告知（不再演示式地凭空加光子） */
  onSelectPackage() {
    wx.showModal({
      title: '光子补给',
      content: '支付通道即将开放。当前可通过每日签到、看视频补给与邀请好友获取光子。',
      showCancel: false,
      confirmText: '我知道了'
    });
  },

  onGoInvite() {
    wx.navigateTo({
      url: '/pages/invite/invite'
    });
  }
});
