const app=getApp(),api=require('../../utils/api.js'),sharing=require('../../utils/template-sharing.js');
const labels={draft:'草稿',pending:'审核中',rejected:'已退回',approved:'已上架',withdrawn:'已撤回'};
Page({
 data:{shareId:'',revision:0,name:'',subtitle:'',prompt:'',advice:'',suitable:'',unsuitable:'',tips:'',guideJson:'',covers:[],status:'draft',statusLabel:'新模板',hasPublished:false,reason:'',consent:false,busy:false,loading:false,error:'',rewardText:'成功使用后的奖励只发给作者，使用者正常扣光子。',locked:false},
 onLoad(options){this._requestId=sharing.requestId();this._tokens=[];if(options&&options.id){this.setData({shareId:decodeURIComponent(options.id)});this.loadShare();}else this.loadPolicy();},
 onShow(){if(this._owner!==undefined&&this._owner!==this.ownerKey())this.clearPrivate();},
 ownerKey(){return app.globalData.userId||'';},
 clearPrivate(){this._tokens=[];this._version=(this._version||0)+1;this.setData({shareId:'',revision:0,name:'',subtitle:'',prompt:'',advice:'',suitable:'',unsuitable:'',tips:'',guideJson:'',covers:[],reason:'',consent:false,locked:true,error:'账号已切换，请重新打开我的模板'});},
 checkOwner(){if(this._unloaded)return false;if(this._owner!==undefined&&this._owner!==this.ownerKey()){this.clearPrivate();return false;}return true;},
 onUnload(){this._unloaded=true;this._version=(this._version||0)+1;},
 async loadPolicy(){try{const r=await api.config(),p=r.template_sharing;if(!this._unloaded&&p)this.setData({rewardText:!p.enabled?'模板分享暂时关闭':!p.reward_enabled?'模板分享开放，作者奖励暂时关闭':`作者奖励默认 ${p.reward_amount} 光子；${p.reward_mode==='all_success'?'每次合格的成功使用':'同一使用者首次成功使用本模板'}可获得奖励。自用不奖励，使用者不额外获赠；受每日额度限制。`});}catch(e){}},
 applyShare(row){
  if(!this.checkOwner())return;
  const g=row.guide||{};this._tokens=(row.cover_tokens||[]).slice();
  const urls=(row.covers||[]).map(x=>typeof x==='string'?x:x.thumbnail_url||x.preview_url||x.url||'');
  this.setData({shareId:row.id,revision:row.revision,name:row.name||'',subtitle:row.subtitle||'',prompt:row.prompt||'',advice:g.advice||'',suitable:(g.suitable||[]).join('\n'),unsuitable:(g.unsuitable||[]).join('\n'),tips:(g.tips||[]).join('\n'),covers:urls.map((url,i)=>({token:this._tokens[i],url:api.absolute(url)})),status:row.status,statusLabel:labels[row.status]||row.status,hasPublished:!!row.has_published,reason:row.reason||'',locked:row.status==='pending',consent:false});
 },
 async loadShare(){const version=(this._version||0)+1;this._version=version;this.setData({loading:true,error:''});try{await api.ensureLogin();this._owner=this.ownerKey();const r=await api.request('/api/template-shares/'+encodeURIComponent(this.data.shareId));if(this.checkOwner()&&version===this._version)this.applyShare(r.submission||r);await this.loadPolicy();}catch(e){if(!this._unloaded)this.setData({error:e.message||'模板读取失败'});}finally{if(!this._unloaded&&version===this._version)this.setData({loading:false});}},
 onField(e){const key=e.currentTarget.dataset.field;if(!this.checkOwner()||!['name','subtitle','prompt','advice','suitable','unsuitable','tips','guideJson'].includes(key)||this.data.busy||this.data.locked)return;this.setData({[key]:e.detail.value,error:''});},
 onConsent(e){this.setData({consent:e.detail.value.includes('template')});},
 onCopyGuide(){if(!this.checkOwner())return;wx.setClipboardData({data:sharing.guidePrompt(this.data),success:()=>wx.showToast({title:'已复制，交给你常用的AI整理',icon:'none'})});},
 onImportGuide(){try{this.setData({...sharing.importGuide(this.data.guideJson),guideJson:'',error:''});wx.showToast({title:'已导入，请核对说明',icon:'none'});}catch(e){this.setData({error:e.message});}},
 async saveDraft(submit=false){
  await api.ensureLogin();
  if(this._owner===undefined)this._owner=this.ownerKey();if(!this.checkOwner())throw new Error('账号已切换，请重新打开我的模板');
  const body=sharing.content(this.data,this._tokens,submit);
  if(submit&&(!body.name||!body.prompt||body.cover_tokens.length<1||!this.data.consent))throw new Error('请填写名称、提示词、至少一张封面，并确认分享授权');
  const id=this.data.shareId;
  const r=await api.request(id?'/api/template-shares/'+encodeURIComponent(id):'/api/template-shares',{method:id?'PUT':'POST',data:id?{revision:this.data.revision,...body}:{request_id:this._requestId,...body}});
  if(!this.checkOwner())return null;const row=r.submission||r;this.applyShare(row);return row;
 },
 async onSaveDraft(){if(this.data.busy||this.data.locked)return;this.setData({busy:true,error:''});try{await this.saveDraft(false);if(!this._unloaded)wx.showToast({title:'草稿已保存',icon:'none'});}catch(e){if(!this._unloaded)this.setData({error:e.message||'保存失败'});}finally{if(!this._unloaded)this.setData({busy:false});}},
 async onSubmit(){if(this.data.busy||this.data.locked)return;this.setData({busy:true,error:''});try{await this.saveDraft(true);if(!this._unloaded)wx.showToast({title:'已提交，等待后台审核',icon:'none'});}catch(e){if(!this._unloaded)this.setData({error:(e.message||'提交暂未确认')+'；请在“我的模板”核对状态，不会因重试另建同一投稿。'});}finally{if(!this._unloaded)this.setData({busy:false});}},
 onChooseCover(){if(this.data.busy||this.data.locked)return;if(this.data.covers.length>=3){wx.showToast({title:'最多3张封面',icon:'none'});return;}wx.chooseMedia({count:3-this.data.covers.length,mediaType:['image'],sourceType:['album'],sizeType:['original','compressed'],success:r=>this.uploadChosen(r.tempFiles||[]),fail:e=>{if(!String(e.errMsg||'').includes('cancel'))wx.showToast({title:'选择封面失败',icon:'none'});}});},
 async uploadChosen(files){
  if(this.data.busy||this.data.locked||this._unloaded)return;this.setData({busy:true,error:''});
  try{let share=await this.saveDraft(false);if(!share)return;
   for(const file of files.slice(0,3-this._tokens.length)){if(!this.checkOwner())return;const r=await sharing.uploadCover(share,file.tempFilePath);if(!this.checkOwner())return;
    if(r.submission){this.applyShare(r.submission);share=r.submission;}else{const cover=r.cover;if(!cover||!cover.token)throw new Error('封面确认数据异常');this._tokens.push(cover.token);this.setData({covers:[...this.data.covers,{token:cover.token,url:api.absolute(cover.thumbnail_url||cover.preview_url)}]});}
   }
  }catch(e){if(!this._unloaded)this.setData({error:e.message||'封面上传失败，可重试或保存草稿'});}finally{if(!this._unloaded)this.setData({busy:false});}
 },
 onRemoveCover(e){if(this.data.busy||this.data.locked)return;const index=Number(e.currentTarget.dataset.index);this._tokens=this._tokens.filter((_,i)=>i!==index);this.setData({covers:this.data.covers.filter((_,i)=>i!==index),consent:false});},
 onPreviewCover(e){const url=e.currentTarget.dataset.url;if(url)wx.previewImage({current:url,urls:this.data.covers.map(c=>c.url)});},
 onMine(){wx.navigateTo({url:'/pages/template-shares/template-shares'});},
 onLibrary(){wx.switchTab({url:'/pages/templates/templates'});}
});
