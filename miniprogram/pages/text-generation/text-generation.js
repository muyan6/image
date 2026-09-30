const api=require('../../utils/api.js');
const app=getApp();
Page({
  data:{prompt:'',ratio:'1:1',ready:false,price:40,busy:false,freeMode:false,
    ratios:[{key:'1:1',label:'方形'},{key:'3:2',label:'横图'},{key:'2:3',label:'竖图'}]},
  onShow(){
    this._visible=true;
    api.config().then(c=>this.setData({ready:!!(c.text_generation&&c.text_generation.ready),
      price:c.text_generation?c.text_generation.price:40,freeMode:!!c.free_mode})).catch(()=>this.setData({ready:false}));
  },
  onHide(){this._visible=false;},
  onUnload(){this._visible=false;this._unloaded=true;},
  onInput(e){this.setData({prompt:(e.detail.value||'').slice(0,500)});},
  onRatio(e){if(!this.data.busy)this.setData({ratio:e.currentTarget.dataset.key});},
  onWorks(){wx.navigateTo({url:'/pages/works/works'});},
  async onGenerate(){
    if(this.data.busy)return;
    if(this._uncertain){wx.showToast({title:'先到作品页核对上次提交，避免重复扣费',icon:'none'});return;}
    if(!this.data.ready){wx.showToast({title:'文生图模型尚未配置',icon:'none'});return;}
    const prompt=this.data.prompt.trim();
    if(!prompt){wx.showToast({title:'请描述你想生成的画面',icon:'none'});return;}
    this.setData({busy:true});
    try{
      const created=await api.request('/api/text-generation',{method:'POST',data:{prompt,aspect_ratio:this.data.ratio}});
      if(!created||!created.job_id)throw new Error('任务响应异常，请在作品页核对');
      const history=app.globalData.historyList||[];
      history.unshift({jobId:created.job_id,status:'processing',quality:'light',templateName:'文字生图',inputMode:'text',
        original:'',result:'',timestamp:Date.now()});
      app.globalData.historyList=history;app.persist();
      if(this._visible!==false&&!this._unloaded)wx.navigateTo({url:'/pages/works/works'});
    }catch(e){
      if(e.code==='NETWORK'||e.status>=500||!e.status)this._uncertain=true;
      if(this._visible!==false&&!this._unloaded)wx.showModal({title:'文生图提交未完成',content:(e.message||'请稍后重试')+(this._uncertain?'；先到作品页核对是否已受理。':''),showCancel:false});
    }finally{if(!this._unloaded)this.setData({busy:false});}
  }
});
