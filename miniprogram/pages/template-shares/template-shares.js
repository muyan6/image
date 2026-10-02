const app=getApp(),api=require('../../utils/api.js');
const labels={draft:'草稿',pending:'审核中',rejected:'已退回',approved:'已上架',withdrawn:'已撤回'};
const coverUrl=x=>typeof x==='string'?x:x&&(x.thumbnail_url||x.preview_url)||'';
Page({
 data:{items:[],loading:false,error:'',hasMore:false,rewards:null},
 onShow(){this._visible=true;if(this._owner!==undefined&&this._owner!==(app.globalData.userId||''))this.setData({items:[],rewards:null});this.loadMine();},onHide(){this._visible=false;},onUnload(){this._unloaded=true;this._version=(this._version||0)+1;},
 onPullDownRefresh(){this.loadMine().finally(()=>wx.stopPullDownRefresh());},
 async loadMine(more=false){more=more===true;if(more&&(this.data.loading||!this.data.hasMore))return;const version=(this._version||0)+1;this._version=version;const offset=more?this._nextOffset||0:0;this.setData({loading:true,error:''});
  try{await api.ensureLogin();const owner=app.globalData.userId||'';this._owner=owner;const r=await api.request('/api/template-shares/mine?limit=24&offset='+offset);if(this._unloaded||version!==this._version)return;if(owner!==(app.globalData.userId||'')){this.setData({items:[],rewards:null,error:'账号已切换，请重新读取'});return;}const items=(r.items||[]).map(x=>({...x,template_id:x.template_id||x.published_template_id,statusLabel:labels[x.status]||x.status,coverUrl:api.absolute(coverUrl((x.covers||[])[0])),updatedLabel:x.updated_at?new Date(x.updated_at*1000).toLocaleDateString():''}));this._nextOffset=r.next_offset;this.setData({items:[...new Map((more?[...this.data.items,...items]:items).map(x=>[x.id,x])).values()],hasMore:!!r.has_more,rewards:r.rewards||null});}
  catch(e){if(!this._unloaded&&version===this._version)this.setData({error:e.message||'我的模板读取失败'});}finally{if(!this._unloaded&&version===this._version)this.setData({loading:false});}
 },
 onMore(){return this.loadMine(true);},onRetry(){return this.loadMine();},onCreate(){wx.navigateTo({url:'/pages/template-share/template-share'});},onEdit(e){wx.navigateTo({url:'/pages/template-share/template-share?id='+encodeURIComponent(e.currentTarget.dataset.id)});},
 onView(e){wx.navigateTo({url:'/pages/style-detail/style-detail?id='+encodeURIComponent(e.currentTarget.dataset.templateId)});},
 onWithdraw(e){const item=this.data.items.find(x=>x.id===e.currentTarget.dataset.id);if(!item||this._withdrawing)return;wx.showModal({title:'撤回模板分享',content:'撤回后停止公开展示和新的使用，已完成的作品保留。',confirmText:'确认撤回',success:async r=>{if(!r.confirm)return;this._withdrawing=true;try{await api.request('/api/template-shares/'+encodeURIComponent(item.id)+'/withdraw',{method:'POST',data:{revision:item.revision}});if(!this._unloaded)await this.loadMine();}catch(e){if(!this._unloaded)wx.showToast({title:e.message||'撤回失败',icon:'none'});}finally{this._withdrawing=false;}}});}
});
