const api=require('../../utils/api');
Page({
  data:{id:'',busy:false,done:false,message:'请确认是否登录网页端。网页端将共用你的光子余额、作品和账户状态。'},
  onLoad(options){
    let id='';try{id=decodeURIComponent(options.scene||'');}catch(e){}
    if(!/^[a-f0-9]{32}$/.test(id)){this.setData({done:true,message:'登录码无效，请在网页刷新后重新扫码。'});return;}
    this.setData({id});
    api.ensureLogin(true).catch(()=>this.setData({done:true,message:'微信登录失败，请重新扫码。'}));
  },
  async choose(action){
    if(this.data.busy||this.data.done)return;
    this.setData({busy:true});
    try{
      await api.ensureLogin();
      await api.request('/api/auth/wechat-web/approve',{method:'POST',data:{id:this.data.id,action}});
      this.setData({done:true,message:action==='approve'?'登录已确认，请返回网页继续操作。':'已取消网页登录。'});
    }catch(e){this.setData({message:e.message||'确认失败，请重试。'});}
    finally{this.setData({busy:false});}
  },
  onApprove(){this.choose('approve');},
  onDeny(){this.choose('deny');}
});
