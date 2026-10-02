const app = getApp();
const api = require('../../utils/api.js');
const payment = require('../../utils/payment.js');
const commerce = require('../../utils/commerce.js');

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
    catalogLoading:true, catalogError:'', catalogStale:false,
    packages: [],
    records: [], recordsLoading:false, recordsLoaded:false, recordsError:'', recordsHasMore:false, recordTotal:0,
    balanceLoaded:false, balanceError:''
  },

  onLoad() {
    // 首次进入由 onShow 统一加载，避免两次相同请求。
  },

  onShow() {
    this._visible=true;this._unloaded=false;
    this.refreshData();
    this.loadRecords();
  },

  onHide() { this._visible=false;this.stopOfferClock(); },
  onUnload() { this._unloaded=true;this.onHide();this._catalogVersion=(this._catalogVersion||0)+1; },
  stopOfferClock() { if(this._offerTimer!=null)clearTimeout(this._offerTimer);this._offerTimer=null; },
  serverNow() { return (Date.now()+(this._serverOffset||0))/1000; },
  refreshCatalog() {
    const version=(this._catalogVersion||0)+1;this._catalogVersion=version;
    this.stopOfferClock();
    this.setData({catalogLoading:true,catalogError:'',paymentReady:false});
    return api.request('/api/credits/packages').then(d=>{
      if(this._unloaded||this._catalogVersion!==version)return false;
      if(!d||!Array.isArray(d.packages))throw new Error('套餐数据异常');
      this._serverOffset=Number.isFinite(d.server_time)?d.server_time*1000-Date.now():0;
      this._offers=d.packages;this._nextChange=Number(d.next_change_at)||0;
      // Older servers do not have scheduling fields; normal offers remain usable.
      const ends=d.packages.filter(p=>p.promotion_active).map(p=>Number(p.promotion_ends_at)).filter(t=>t>0);
      if(!this._nextChange&&ends.length)this._nextChange=Math.min(...ends);
      const cost=typeof d.generation_cost==='number'?d.generation_cost:40;
      const stale=this._nextChange>0&&this.serverNow()>=this._nextChange;
      this.setData({packages:commerce.decorate(this._offers,this.serverNow()),catalogLoading:false,catalogStale:stale,
        paymentReady:!!d.payment_ready&&!stale,costPerGeneration:cost,
        estimatedGenerations:cost>0?Math.floor(app.globalData.lightPoints/cost):0});
      this._retryAt=stale?Date.now()+15000:0;this.startOfferClock();return !stale;
    }).catch(()=>{
      if(this._unloaded||this._catalogVersion!==version)return false;
      this.setData({catalogLoading:false,catalogError:'套餐加载失败，点击重新加载',paymentReady:false});
      this._retryAt=Date.now()+15000;this.startOfferClock();return false;
    });
  },
  startOfferClock() {
    this.stopOfferClock();if(this._visible===false||this._unloaded)return;
    if(!this._nextChange&&!this._retryAt)return;
    this._offerTimer=setTimeout(()=>{
      this._offerTimer=null;if(this._visible===false||this._unloaded)return;
      const now=this.serverNow();
      if(this._nextChange>0&&now>=this._nextChange){
        this.setData({catalogStale:true,paymentReady:false,packages:commerce.decorate(this._offers||[],now)});
        if(!this._retryAt||Date.now()>=this._retryAt){this.refreshCatalog();return;}
      }else if(this._offers)this.setData({packages:commerce.decorate(this._offers,now)});
      if(this._retryAt&&Date.now()>=this._retryAt){this.refreshCatalog();return;}
      this.startOfferClock();
    },1000);
  },

  refreshData() {
    this.setData({ lightPoints: app.globalData.lightPoints,
      estimatedGenerations: this.data.costPerGeneration > 0
        ? Math.floor(app.globalData.lightPoints / this.data.costPerGeneration) : 0 });

    this.refreshCatalog();

    api.config().then((c) => {
      if (c) this.setData({ freeMode: !!c.free_mode,
        inviteReward: c.rewards && typeof c.rewards.invite === 'number' ? c.rewards.invite : this.data.inviteReward,
        checkinReward: c.rewards && typeof c.rewards.checkin === 'number' ? c.rewards.checkin : this.data.checkinReward,
        checkinBonus: c.rewards && typeof c.rewards.checkin_seventh_bonus === 'number'
          ? c.rewards.checkin_seventh_bonus : this.data.checkinBonus });
    }).catch(() => {});

    // 签到状态与余额都以服务端为准
    const version=(this._balanceVersion||0)+1;this._balanceVersion=version;
    this.setData({balanceError:''});
    return api.me().then((d) => {
      if(this._unloaded||version!==this._balanceVersion)return;
      if (!d) return;
      if (typeof d.balance === 'number') app.setBalance(d.balance);
      this.setData({
        lightPoints: app.globalData.lightPoints, balanceLoaded:true,
        estimatedGenerations: this.data.costPerGeneration > 0
          ? Math.floor(app.globalData.lightPoints / this.data.costPerGeneration) : 0
      });
      this.renderCheckin(d.earn || {});
    }).catch(() => {if(!this._unloaded&&version===this._balanceVersion)this.setData({balanceError:'余额更新失败，请重试'});});
  },

  async loadRecords(more=false) {
    more=more===true;
    if(more&&(this.data.recordsLoading||!this.data.recordsHasMore))return;
    const version=(this._recordsVersion||0)+1;this._recordsVersion=version;
    const offset=more?(this._recordsOffset||0):0;
    this.setData({recordsLoading:true,recordsError:''});
    try {
      const d=await api.request('/api/me/credits?limit=30&offset='+offset);
      if(this._unloaded||version!==this._recordsVersion)return;
      if(!d||!Array.isArray(d.items))throw new Error('光子明细数据异常');
      const items=d.items.map(item=>{
        const date=new Date(item.created_at*1000),pad=n=>String(n).padStart(2,'0');
        return {...item, amountText:(item.amount>0?'+':'')+item.amount,
          time:`${date.getFullYear()}.${pad(date.getMonth()+1)}.${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`};
      });
      this._recordsOffset=d.next_offset;
      this.setData({records:[...new Map((more?this.data.records.concat(items):items).map(x=>[x.id,x])).values()],
        recordTotal:d.total,recordsHasMore:!!d.has_more,recordsLoaded:true});
    }catch(e){if(!this._unloaded&&version===this._recordsVersion)this.setData({recordsError:e.message||'光子明细读取失败，请重试'});}
    finally{if(!this._unloaded&&version===this._recordsVersion)this.setData({recordsLoading:false});}
  },
  onMoreRecords(){return this.loadRecords(true);},
  onRetryRecords(){return this.loadRecords();},
  onOpenRecord(e){
    const row=this.data.records.find(x=>String(x.id)===String(e.currentTarget.dataset.id));if(!row)return;
    if(row.order_id)wx.navigateTo({url:'/pages/orders/orders?id='+encodeURIComponent(row.order_id)});
    else if(row.job_id)wx.navigateTo({url:'/pages/works/works?jobId='+encodeURIComponent(row.job_id)});
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
        this.setData({lightPoints: app.globalData.lightPoints,balanceLoaded:typeof d.balance==='number'||this.data.balanceLoaded,balanceError:'',
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
    const id=e.currentTarget.dataset.id;
    const shown=this.data.packages.find(p=>p.id===id);if(!shown)return;
    this.setData({paymentBusy:true});
    try{
      // Refresh before the confirmation dialog. Old offer IDs are rejected server-side too.
      if(!await this.refreshCatalog())return;
      if(this._visible===false||this._unloaded||!this.data.paymentReady)return;
      const pkg=this.data.packages.find(p=>p.id===id);
      if(!pkg){wx.showToast({title:'套餐或活动已更新，请重新选择',icon:'none'});return;}
      const result=await payment.buy(pkg);
      if(result.orderId&&!this._unloaded&&this._visible!==false)wx.navigateTo({url:'/pages/orders/orders?id='+encodeURIComponent(result.orderId)});
    }catch(err){if(!this._unloaded)wx.showModal({title:err.canceled?'支付已取消':'订单状态',content:err.message+'；已创建的订单可在充值订单中查看。',showCancel:false});}
    finally{if(!this._unloaded)this.setData({paymentBusy:false});}
  },
  onGoOrders(){wx.navigateTo({url:'/pages/orders/orders'});},

  onGoInvite() {
    wx.navigateTo({
      url: '/pages/invite/invite'
    });
  }
});
