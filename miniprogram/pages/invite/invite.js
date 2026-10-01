const app = getApp();
const api = require('../../utils/api.js');

Page({
  data: {
    inviteCode: '',
    inviteError: false,
    inviteReward: 40,
    records: [], total: 0, recordedReward: 0, historicalUnknown: 0, recordsLoaded: false,
    recordsLoading: false, recordsError: '', hasMore: false
  },

  onLoad() {
    this.loadInviteCode();
    if (typeof api.config === 'function') api.config().then(c => {
      if (c && c.rewards) this.setData({inviteReward: c.rewards.invite});
    }).catch(() => {});
  },

  onShow() {
    if (!this.data.inviteCode) this.loadInviteCode();
    return this.loadRecords();
  },

  onUnload(){this._unloaded=true;},
  onPullDownRefresh(){return this.loadRecords().finally(()=>wx.stopPullDownRefresh());},
  async loadRecords(more=false){
    more=more===true;if(this._recordsBusy||(more&&!this.data.hasMore))return;
    this._recordsBusy=true;this.setData({recordsLoading:true,recordsError:''});
    try{
      const d=await api.request('/api/me/invites?limit=30&offset='+(more?(this._offset||0):0));
      if(this._unloaded)return;
      const records=(d.items||[]).map(x=>{
        const date=new Date(x.bound_at*1000),pad=n=>String(n).padStart(2,'0');
        return {...x,dateLabel:`${date.getFullYear()}.${pad(date.getMonth()+1)}.${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`};
      });
      this._offset=d.next_offset;this.setData({records:more?[...new Map([...this.data.records,...records].map(x=>[x.id,x])).values()]:records,
        total:d.total,recordedReward:d.recorded_reward,historicalUnknown:d.historical_unknown,hasMore:!!d.has_more,recordsLoaded:true});
    }catch(e){if(!this._unloaded)this.setData({recordsError:e.message||'邀请明细读取失败'});}
    finally{this._recordsBusy=false;if(!this._unloaded)this.setData({recordsLoading:false});}
  },
  onRetryRecords(){return this.loadRecords();},
  onMoreRecords(){return this.loadRecords(true);},

  loadInviteCode() {
    if (this._loadingCode) return;
    this._loadingCode = true;
    api.me()
      .then((d) => {
        const code = (d && d.invite_code) || '';
        if (!/^INV-[0-9A-F]{8}$/.test(code)) throw new Error('邀请码尚未就绪');
        this.setData({ inviteCode: code, inviteError: false });
      })
      .catch(() => {
        this.setData({ inviteCode: '', inviteError: true });
      }).finally(() => { this._loadingCode = false; });
  },

  onCopyCode() {
    if (!/^INV-[0-9A-F]{8}$/.test(this.data.inviteCode)) {
      this.loadInviteCode();
      wx.showToast({ title: '邀请码尚未获取，请稍后重试', icon: 'none' });
      return;
    }
    wx.setClipboardData({
      data: this.data.inviteCode,
      success: () => {
        wx.showToast({ title: '邀请码已复制', icon: 'success' });
      }
    });
  },

  onShareAppMessage() {
    if (!/^INV-[0-9A-F]{8}$/.test(this.data.inviteCode)) {
      return {title: '来废片新生所拯救你的旧照片！', path: '/pages/index/index'};
    }
    return {
      title: `送你 ✦${this.data.inviteReward} 光子，来废片新生所重塑旧照片！`,
      path: '/pages/index/index?invite=' + this.data.inviteCode,
      imageUrl: '/images/logo.jpg'
    };
  }
});
