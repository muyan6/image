const api=require('../../utils/api.js');
const creation=require('../../utils/creation-draft.js');
const app=getApp();
Page({
  data:{prompt:'',ratio:'1:1',ready:false,price:40,busy:false,freeMode:false,lightPoints:0,configLoading:true,configError:'',
    draftAvailable:false,submissionPending:false,submissionMessage:'',retryableSubmission:false,
    examples:[{label:'水彩风景',prompt:'雨后的森林小屋，暖色灯光，水彩插画风格。'},
      {label:'简约壁纸',prompt:'暖白色背景，一枝浅绿色植物，柔和自然光，简约手机壁纸。'},
      {label:'旅行插画',prompt:'海边小镇的午后，蓝白房屋与橘色屋顶，清新旅行插画。'}],
    ratios:[{key:'1:1',label:'方形'},{key:'3:2',label:'横图'},{key:'2:3',label:'竖图'}]},
  onLoad(options){
    const draft=creation.readDraft();
    if(options&&options.restoreDraft&&draft&&draft.input_mode==='text')this.restoreDraft(draft);
    else this.setData({draftAvailable:!!(draft&&draft.input_mode==='text')});
  },
  onShow(){
    this._visible=true;this.setData({lightPoints:app.globalData.lightPoints||0});
    const config=this.loadConfig(),balance=this.loadBalance();
    const pending=creation.readPendingSubmission();
    if(pending){
      if(pending.input_mode==='text')this.setData({prompt:pending.recipe.prompt,ratio:pending.recipe.aspect_ratio});
      this.setData({submissionPending:true,retryableSubmission:false,submissionMessage:'正在确认上次提交，确认前不会再次扣费'});
      this.onConfirmSubmission();
    }
    return Promise.all([config,balance]);
  },
  loadBalance(){return api.me().then(d=>{if(d&&typeof d.balance==='number')app.setBalance(d.balance);if(!this._unloaded)this.setData({lightPoints:app.globalData.lightPoints||0});}).catch(()=>{});},
  loadConfig(){
    const version=(this._configVersion||0)+1;this._configVersion=version;
    this.setData({configLoading:true,configError:'',ready:false});
    return api.config().then(c=>{
      if(this._unloaded||version!==this._configVersion)return;
      if(!c||!c.text_generation||!Number.isInteger(c.text_generation.price))throw new Error('empty config');
      this.setData({ready:!!c.text_generation.ready,price:c.text_generation.price,freeMode:!!c.free_mode});
    }).catch(()=>{if(!this._unloaded&&version===this._configVersion)this.setData({ready:false,configError:'生成配置加载失败，请重试'});})
      .finally(()=>{if(!this._unloaded&&version===this._configVersion)this.setData({configLoading:false});});
  },
  restoreDraft(draft){this.setData({prompt:draft.prompt||'',ratio:draft.aspect_ratio||'1:1',draftAvailable:false});},
  onResumeDraft(){const draft=creation.readDraft();if(draft&&draft.input_mode==='text')this.restoreDraft(draft);},
  saveDraft(){if(this._completed||!this.data.prompt.trim())return Promise.resolve();return creation.saveDraft({input_mode:'text',prompt:this.data.prompt,aspect_ratio:this.data.ratio}).catch(()=>{});},
  onHide(){this._visible=false;this.saveDraft();},
  onUnload(){this._visible=false;this.saveDraft();this._unloaded=true;},
  onInput(e){if(this.data.submissionPending)return;this._completed=false;this.setData({prompt:(e.detail.value||'').slice(0,500)});},
  onUseExample(e){
    if(this.data.busy||this.data.submissionPending)return;
    const example=this.data.examples[e.currentTarget.dataset.index];if(!example)return;
    const fill=()=>{this._completed=false;this.setData({prompt:example.prompt});};
    if(this.data.prompt.trim())wx.showModal({title:'使用示例描述',content:'将替换当前描述，填入后仍可修改。',confirmText:'替换',success:r=>{if(r.confirm&&!this.data.busy&&!this._unloaded)fill();}});
    else fill();
  },
  onClearPrompt(){if(!this.data.busy&&!this.data.submissionPending){this.setData({prompt:''});creation.clearDraft().catch(()=>{});}},
  onRatio(e){if(!this.data.busy&&!this.data.submissionPending){this._completed=false;this.setData({ratio:e.currentTarget.dataset.key});}},
  onWorks(){wx.navigateTo({url:'/pages/works/works'});},
  onCredits(){wx.navigateTo({url:'/pages/credits/credits'});},
  acceptSubmission(created,requestId,pendingHint){
    const pending=pendingHint||creation.readPendingSubmission();const inputMode=pending?pending.input_mode:'text';
    creation.resolveSubmission(requestId);this._uncertain=false;this._completed=true;creation.clearDraft().catch(()=>{});
    if(typeof created.balance==='number')app.setBalance(created.balance);
    const history=app.globalData.historyList||[];
    const form=pending&&(pending.recipe.form||pending.recipe);
    if(!history.some(h=>h.jobId===created.job_id))history.unshift({jobId:created.job_id,status:created.status||'processing',quality:form&&form.quality||'light',templateName:inputMode==='text'?'文字生图':(pending.recipe.template_name||''),inputMode,original:'',result:'',timestamp:Date.now()});
    if(inputMode==='photo'&&form&&form.template_id)api.request('/api/me/recent-template',{method:'POST',data:{template_id:form.template_id}}).catch(()=>{});
    app.globalData.historyList=history;app.persist();
    if(!this._unloaded)this.setData({submissionPending:false,retryableSubmission:false,submissionMessage:'',lightPoints:app.globalData.lightPoints||0});
    if(this._visible!==false&&!this._unloaded)wx.navigateTo({url:'/pages/works/works'});
  },
  async onConfirmSubmission(){
    if(this._checkingSubmission)return;this._checkingSubmission=true;
    try{
      const result=await creation.reconcileSubmission();if(!result||this._unloaded)return;
      if(result.state==='accepted'&&result.job_id){this.acceptSubmission(result,result.pending.client_request_id,result.pending);return;}
      if(result.state==='rejected'){this._uncertain=false;this.setData({submissionPending:false,retryableSubmission:false,submissionMessage:result.detail||'上次提交未受理，可重新生成'});this.loadConfig();this.loadBalance();return;}
      if(result.state==='not_found'&&result.pending.input_mode==='text'){this.setData({submissionPending:true,retryableSubmission:true,submissionMessage:'未查到提交登记，可沿用原编号重新提交；不会创建重复任务'});return;}
      this.setData({submissionPending:true,retryableSubmission:false,submissionMessage:result.pending.input_mode==='text'?'云端正在确认上次提交，稍后再核对；不会再次扣费':'还有照片提交待确认，请先到作品页核对'});
    }catch(e){if(!this._unloaded)this.setData({submissionPending:true,retryableSubmission:false,submissionMessage:'网络尚未恢复，上次提交仍待确认；不会再次扣费'});}
    finally{this._checkingSubmission=false;}
  },
  async onGenerate(){
    if(this.data.busy)return;
    if(this.data.submissionPending&&!this.data.retryableSubmission){this.onConfirmSubmission();return;}
    if(!this.data.ready){wx.showToast({title:'请先加载生成配置',icon:'none'});return;}
    const prompt=this.data.prompt.trim();if(!prompt){wx.showToast({title:'请描述你想生成的画面',icon:'none'});return;}
    const existing=creation.readPendingSubmission();
    if(existing&&existing.input_mode!=='text'){this.onConfirmSubmission();return;}
    let recipe=existing?existing.recipe:{prompt,aspect_ratio:this.data.ratio,expected_price:this.data.freeMode?0:this.data.price};
    const cost=this.data.freeMode?0:this.data.price;
    if(!this.data.freeMode&&this.data.lightPoints<cost){wx.showModal({title:'光子余额不足',content:`本次预扣 ${cost} 光子，当前余额 ${this.data.lightPoints}。是否前往补给？`,confirmText:'补充光子',success:r=>{if(r.confirm)this.onCredits();}});return;}
    this.setData({busy:true});let pending;
    this._completed=false;
    try{
      await api.ensureLogin();await this.saveDraft();
      if(existing){creation.updateRetryableSubmission({expected_price:this.data.freeMode?0:this.data.price});recipe=creation.readPendingSubmission().recipe;}
      pending=creation.beginSubmission('text',recipe);
      if(this._unloaded){creation.resolveSubmission(pending.client_request_id);return;}
      const created=await api.request('/api/text-generation',{method:'POST',data:Object.assign({},recipe,{client_request_id:pending.client_request_id})});
      if(!created||!created.job_id){const error=new Error('任务响应异常，正在确认是否受理');error.jobSubmissionAttempted=true;throw error;}
      this.acceptSubmission(created,pending.client_request_id);
    }catch(e){
      if(e.jobSubmissionAttempted){this._uncertain=true;if(!this._unloaded)this.setData({submissionPending:true,retryableSubmission:false,submissionMessage:'提交响应中断，正在确认；不会再次扣费'});this.onConfirmSubmission();}
      else if(existing){
        if(e.status===409)this.loadConfig();
        if(!this._unloaded)this.setData({submissionPending:true,retryableSubmission:false,submissionMessage:'原编号重试未完成，正在重新确认提交；不会创建新任务'});
        await this.onConfirmSubmission();
      }else {
        if(pending)creation.resolveSubmission(pending.client_request_id);
        if(!this._unloaded)this.setData({submissionPending:false,retryableSubmission:false,submissionMessage:''});
        if(e.status===409)this.loadConfig();if(e.status===402)this.loadBalance();
        if(this._visible!==false&&!this._unloaded){
          if(e.status===402)wx.showModal({title:'光子余额不足',content:e.message||'余额不足，请补充光子',confirmText:'补充光子',success:r=>{if(r.confirm)this.onCredits();}});
          else wx.showModal({title:'文生图提交未完成',content:e.message||'请稍后重试',showCancel:false});
        }
      }
    }finally{if(!this._unloaded)this.setData({busy:false});}
  }
});
