const app = getApp();
const api = require('../../utils/api.js');

Page({
  data: {
    lightPoints: 0,
    freeMode: false,
    checkedIn: false,
    costPerGeneration: 40,
    estimatedGenerations: 0,
    paymentReady: false,
    packages: [
      {id:'points_600',points:600,price_text:'¥6',bonus_text:'标准 1 元 = 100 光子',generations:15},
      {id:'points_3300',points:3300,price_text:'¥30',bonus_text:'多送 300 光子',generations:82},
      {id:'points_8160',points:8160,price_text:'¥68',bonus_text:'多送 1360 光子',generations:204},
      {id:'points_16640',points:16640,price_text:'¥128',bonus_text:'多送 3840 光子',generations:416}
    ],
    records: []
  },

  onLoad() {
    this.refreshData();
  },

  onShow() {
    this.refreshData();
  },

  refreshData() {
    this.setData({ lightPoints: app.globalData.lightPoints,
      estimatedGenerations: Math.floor(app.globalData.lightPoints / this.data.costPerGeneration) });

    api.request('/api/credits/packages').then((d) => {
      if (!d || !Array.isArray(d.packages)) return;
      const cost = d.generation_cost || 40;
      this.setData({packages: d.packages, paymentReady: !!d.payment_ready,
        costPerGeneration: cost,
        estimatedGenerations: Math.floor(app.globalData.lightPoints / cost)});
    }).catch(() => {});

    api.config().then((c) => {
      if (c) this.setData({ freeMode: !!c.free_mode });
    }).catch(() => {});

    // 签到状态与余额都以服务端为准
    api.me().then((d) => {
      if (!d) return;
      if (typeof d.balance === 'number') app.setBalance(d.balance);
      this.setData({
        lightPoints: app.globalData.lightPoints,
        estimatedGenerations: Math.floor(app.globalData.lightPoints / this.data.costPerGeneration),
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
          lightPoints: app.globalData.lightPoints,
          estimatedGenerations: Math.floor(app.globalData.lightPoints / this.data.costPerGeneration)
        });
        wx.showToast({ title: '签到成功 ✦10 光子', icon: 'success' });
      })
      .catch((err) => {
        wx.showToast({ title: err.message || '签到失败，稍后再试', icon: 'none' });
      });
  },

  /** 充值套餐：服务端未完成支付验单与发货前，不会凭前端点击加点。 */
  onSelectPackage() {
    wx.showModal({
      title: '光子补给',
      content: '套餐价格已确定。请先在微信虚拟支付后台上架对应商品并配置发货推送；支付验单功能上线前不会扣款或发放光子。',
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
