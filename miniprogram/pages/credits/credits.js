const app = getApp();
const api = require('../../utils/api.js');
const payment = require('../../utils/payment.js');

Page({
  data: {
    lightPoints: 0,
    freeMode: false,
    checkedIn: false,
    showCheckinSheet: false,
    checkinBusy: false,
    checkinReward: 10,
    checkinBonus: 30,
    checkinStreak: 0,
    checkinProgress: 0,
    checkinDay: 1,
    checkinDays: [],
    inviteReward: 40,
    costPerGeneration: 40,
    estimatedGenerations: 0,
    paymentReady: false,
    paymentBusy: false,
    packages: [
      {id:'points_600',points:600,price_text:'¥6',bonus_text:'标准 1 元 = 100 光子',generations:15},
      {id:'points_3300',points:3300,price_text:'¥30',bonus_text:'多送 300 光子',generations:82},
      {id:'points_8160',points:8160,price_text:'¥68',bonus_text:'多送 1360 光子',generations:204},
      {id:'points_16640',points:16640,price_text:'¥128',bonus_text:'多送 3840 光子',generations:416}
    ],
    records: []
  },

  onLoad() {
    // 首次进入由 onShow 统一加载，避免两次相同请求。
  },

  onShow() {
    this.refreshData();
  },

  refreshData() {
    this.setData({ lightPoints: app.globalData.lightPoints,
      estimatedGenerations: this.data.costPerGeneration > 0
        ? Math.floor(app.globalData.lightPoints / this.data.costPerGeneration) : 0 });

    api.request('/api/credits/packages').then((d) => {
      if (!d || !Array.isArray(d.packages)) return;
      const cost = typeof d.generation_cost === 'number' ? d.generation_cost : 40;
      this.setData({packages: d.packages, paymentReady: !!d.payment_ready,
        costPerGeneration: cost,
        estimatedGenerations: cost > 0 ? Math.floor(app.globalData.lightPoints / cost) : 0});
    }).catch(() => {});

    api.config().then((c) => {
      if (c) this.setData({ freeMode: !!c.free_mode,
        inviteReward: c.rewards && typeof c.rewards.invite === 'number' ? c.rewards.invite : this.data.inviteReward,
        checkinReward: c.rewards && typeof c.rewards.checkin === 'number' ? c.rewards.checkin : this.data.checkinReward,
        checkinBonus: c.rewards && typeof c.rewards.checkin_seventh_bonus === 'number'
          ? c.rewards.checkin_seventh_bonus : this.data.checkinBonus });
    }).catch(() => {});

    // 签到状态与余额都以服务端为准
    api.me().then((d) => {
      if (!d) return;
      if (typeof d.balance === 'number') app.setBalance(d.balance);
      this.setData({
        lightPoints: app.globalData.lightPoints,
        estimatedGenerations: this.data.costPerGeneration > 0
          ? Math.floor(app.globalData.lightPoints / this.data.costPerGeneration) : 0
      });
      this.renderCheckin(d.earn || {});
    }).catch(() => {});
  },

  renderCheckin(earn) {
    const done = !!earn.checkin_done;
    const progress = Math.max(0, Math.min(7, Number(earn.checkin_progress) || 0));
    const day = Math.max(1, Math.min(7, Number(earn.checkin_day) || 1));
    this.setData({
      checkedIn: done,
      checkinStreak: Number(earn.checkin_streak) || 0,
      checkinProgress: progress,
      checkinDay: day,
      checkinReward: typeof earn.checkin_reward === 'number' ? earn.checkin_reward : this.data.checkinReward,
      checkinBonus: typeof earn.checkin_seventh_bonus === 'number' ? earn.checkin_seventh_bonus : this.data.checkinBonus,
      checkinDays: [1,2,3,4,5,6,7].map(n => ({day:n, done:n <= progress,
        today:!done && n === day, bonus:n === 7}))
    });
  },

  onOpenCheckin() { this.setData({showCheckinSheet:true}); },
  onCloseCheckin() { this.setData({showCheckinSheet:false}); },

  /** 每日签到：奖励和连续七日加奖由服务端原子记账。 */
  onCheckIn() {
    if (this.data.checkinBusy) return;
    if (this.data.checkedIn) {
      wx.showToast({ title: '今日已签到', icon: 'none' });
      return;
    }
    this.setData({checkinBusy:true});
    api.earn('checkin')
      .then((d) => {
        if (d && typeof d.balance === 'number') app.setBalance(d.balance);
        this.setData({lightPoints: app.globalData.lightPoints,
          estimatedGenerations: this.data.costPerGeneration > 0
            ? Math.floor(app.globalData.lightPoints / this.data.costPerGeneration) : 0});
        this.renderCheckin({checkin_done:true,checkin_streak:d.checkin_streak,
          checkin_progress:d.checkin_progress,checkin_day:d.checkin_progress});
        wx.showToast({ title: `签到成功 ✦${d.reward}`, icon: 'none' });
      })
      .catch((err) => {
        wx.showToast({ title: err.message || '签到失败，稍后再试', icon: 'none' });
      }).finally(() => this.setData({checkinBusy:false}));
  },

  /** 充值套餐：服务端未完成支付验单与发货前，不会凭前端点击加点。 */
  async onSelectPackage(e) {
    if(this.data.paymentBusy)return;
    if(!this.data.paymentReady){wx.showModal({title:'光子充值',content:'充值暂未启用，请联系管理员确认新版后端和支付配置。',showCancel:false});return;}
    const pkg=this.data.packages.find(p=>p.id===e.currentTarget.dataset.id);if(!pkg)return;
    this.setData({paymentBusy:true});
    try{
      const result=await payment.buy(pkg);
      if(result.orderId)wx.navigateTo({url:'/pages/orders/orders?id='+encodeURIComponent(result.orderId)});
    }catch(err){wx.showModal({title:err.canceled?'支付已取消':'订单状态',content:err.message+'；已创建的订单可在充值订单中查看。',showCancel:false});}
    finally{this.setData({paymentBusy:false});}
  },
  onGoOrders(){wx.navigateTo({url:'/pages/orders/orders'});},

  onGoInvite() {
    wx.navigateTo({
      url: '/pages/invite/invite'
    });
  }
});
