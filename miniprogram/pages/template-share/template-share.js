const app=getApp(),api=require('../../utils/api.js'),sharing=require('../../utils/template-sharing.js');
const labels={draft:'草稿',pending:'审核中',rejected:'已退回',approved:'已上架',withdrawn:'已撤回'};
Page({
 data:{shareId:'',revision:0,name:'',subtitle:'',prompt:'',advice:'',suitable:'',unsuitable:'',tips:'',guideJson:'',covers:[],status:'draft',statusLabel:'新模板',hasPublished:false,reason:'',consent:false,busy:false,loading:false,error:'',rewardTitle:'作者奖励',rewardText:'正在读取奖励规则…',rewardRules:'',policyError:'',policyLoading:false,importStatus:'',importError:'',importPending:false,copying:false,copyStatus:'',copyError:'',showAiInstruction:false,aiInstruction:'',showDetails:false,locked:false},
 onLoad(options){this._requestId=sharing.requestId();this._tokens=[];if(this.ownerKey())this._owner=this.ownerKey();if(options&&options.id){this.setData({shareId:decodeURIComponent(options.id)});this.loadShare();}else this.loadPolicy();},
 onShow(){if(this._owner!==undefined&&this._owner!==this.ownerKey())this.clearPrivate();},
 ownerKey(){return app.globalData.userId||'';},
 clearPrivate(){this._tokens=[];this._version=(this._version||0)+1;this._editVersion=(this._editVersion||0)+1;this._copyVersion=(this._copyVersion||0)+1;clearTimeout(this._copyTimer);this._lastImportedText='';this.setData({shareId:'',revision:0,name:'',subtitle:'',prompt:'',advice:'',suitable:'',unsuitable:'',tips:'',guideJson:'',covers:[],reason:'',consent:false,copying:false,copyStatus:'',copyError:'',aiInstruction:'',showAiInstruction:false,importStatus:'',importError:'',importPending:false,locked:true,error:'账号已切换，请重新打开我的模板'});},
 checkOwner(){if(this._unloaded)return false;if(this._owner!==undefined&&this._owner!==this.ownerKey()){this.clearPrivate();return false;}return true;},
 onUnload(){this._unloaded=true;this._version=(this._version||0)+1;this._copyVersion=(this._copyVersion||0)+1;clearTimeout(this._copyTimer);},
 async loadPolicy(){const version=(this._policyVersion||0)+1;this._policyVersion=version;this.setData({policyLoading:true,policyError:''});try{const r=await api.config(),copy=sharing.rewardCopy(r.template_sharing);if(!this._unloaded&&version===this._policyVersion)this.setData(copy);}catch(e){if(!this._unloaded&&version===this._policyVersion)this.setData({rewardTitle:'作者奖励',rewardText:'奖励规则暂未读取，请重试。',rewardRules:'',policyError:'奖励金额和条件以后台配置为准'});}finally{if(!this._unloaded&&version===this._policyVersion)this.setData({policyLoading:false});}},
 applyShare(row){
  if(!this.checkOwner())return;
  this._editVersion=(this._editVersion||0)+1;
  const g=row.guide||{};this._tokens=(row.cover_tokens||[]).slice();
  const urls=(row.covers||[]).map(x=>typeof x==='string'?x:x.thumbnail_url||x.preview_url||x.url||'');
  this.setData({shareId:row.id,revision:row.revision,name:row.name||'',subtitle:row.subtitle||'',prompt:row.prompt||'',advice:g.advice||'',suitable:(g.suitable||[]).join('\n'),unsuitable:(g.unsuitable||[]).join('\n'),tips:(g.tips||[]).join('\n'),covers:urls.map((url,i)=>({token:this._tokens[i],url:api.absolute(url)})),status:row.status,statusLabel:labels[row.status]||row.status,hasPublished:!!row.has_published,reason:row.reason||'',locked:row.status==='pending',consent:false});
 },
 async loadShare(){const version=(this._version||0)+1;this._version=version;this.setData({loading:true,error:''});try{await api.ensureLogin();this._owner=this.ownerKey();const r=await api.request('/api/template-shares/'+encodeURIComponent(this.data.shareId));if(this.checkOwner()&&version===this._version)this.applyShare(r.submission||r);await this.loadPolicy();}catch(e){if(!this._unloaded)this.setData({error:e.message||'模板读取失败'});}finally{if(!this._unloaded&&version===this._version)this.setData({loading:false});}},
 onField(e){const key=e.currentTarget.dataset.field;if(!this.checkOwner()||!['name','subtitle','prompt','advice','suitable','unsuitable','tips','guideJson'].includes(key)||this.data.busy||this.data.locked||this.data.loading)return;this.setData({[key]:e.detail.value,error:'',consent:false});if(key==='guideJson'){this.setData({importError:'',importStatus:'',importPending:false});if(String(e.detail.value||'').trim())this.onImportGuide({auto:true});else this._lastImportedText='';}else{this._editVersion=(this._editVersion||0)+1;if(this.data.importStatus||this.data.importError)this.setData({importStatus:this.data.importPending||this.data.importError?'下方已手动修改；清空粘贴区可使用这些内容，或重新读取AI回复。':'已手动修改，提交以下方内容为准。'});}},
 onConsent(e){this.setData({consent:e.detail.value.includes('template')});},
 onToggleDetails(){if(this.checkOwner())this.setData({showDetails:!this.data.showDetails});},
 onShowAiInstruction(){if(!this.checkOwner())return;this.setData({showAiInstruction:!this.data.showAiInstruction,aiInstruction:sharing.guidePrompt(this.data)});},
 onCopyGuide(){
  if(!this.checkOwner()||this.data.copying||this.data.busy||this.data.loading)return;
  const text=sharing.guidePrompt(this.data),owner=this.ownerKey(),version=(this._copyVersion||0)+1;this._copyVersion=version;let settled=false;
  this.setData({copying:true,copyStatus:'正在复制…',copyError:'',aiInstruction:text});
  const finish=(ok)=>{if(settled)return;settled=true;clearTimeout(this._copyTimer);if(this._unloaded||version!==this._copyVersion)return;if(owner!==this.ownerKey()){this.setData({copying:false,copyStatus:'',copyError:'',showAiInstruction:false,aiInstruction:''});this.checkOwner();return;}if(!this.checkOwner())return;
   this.setData({copying:false,copyStatus:ok?'已复制。把指令和原始模板提示词发给你常用的AI。':'',copyError:ok?'':'复制未完成，指令已展开，可在下方长按选择并复制。',...(ok?{}:{showAiInstruction:true})});
   wx.showToast({title:ok?'AI整理指令已复制':'请长按下方指令复制',icon:'none'});
  };
  this._copyTimer=setTimeout(()=>finish(false),5000);
  try{wx.setClipboardData({data:text,success:()=>finish(true),fail:()=>finish(false)});}catch(e){finish(false);}
 },
 onImportGuide(options={}){
  if(!this.checkOwner()||this.data.busy||this.data.locked||this.data.loading)return;
  const text=String(this.data.guideJson||'').trim(),auto=options.auto===true;
  if(text&&text===this._lastImportedText){this.setData({importError:'',importStatus:'这份内容已读取，保留下方的手动修改。',importPending:false});return;}
  let parsed;try{parsed=sharing.importTemplate(text);}catch(e){this.setData({importError:(e.message||'读取失败，请检查JSON格式')+'；也可清空粘贴区后手动填写。',importStatus:'',importPending:false});return;}
  const apply=()=>{this._editVersion=(this._editVersion||0)+1;this._lastImportedText=text;this.setData({...parsed.fields,consent:false,error:'',importError:'',importPending:false,importStatus:parsed.kind==='template'?'已填入名称、介绍、提示词及选图指南；下方仍可修改。':'已读取旧版选图指南；名称和生成提示词保持原样，请继续补充。'});if(!auto)wx.showToast({title:'已读取，请核对后提交',icon:'none'});};
  const replacing=Object.keys(parsed.fields).some(k=>String(this.data[k]||'').trim()&&this.data[k]!==parsed.fields[k]);
  if(!replacing){apply();return;}
  this.setData({importError:'',importPending:true,importStatus:'内容已识别。点击“替换填入”可更新下方文字，已上传封面保留。'});if(auto||this._importConfirming)return;
  const version=this._editVersion||0,owner=this.ownerKey();this._importConfirming=true;
  const done=()=>{this._importConfirming=false;};
  try{wx.showModal({title:'替换下方模板文字？',content:'将替换AI回复中的名称、介绍、提示词及选图指南。已有封面保留；提交前仍可修改。',confirmText:'替换填入',success:r=>{done();if(!r.confirm||this._unloaded||!this.checkOwner()||owner!==this.ownerKey()||this.data.busy||this.data.locked||this.data.loading||version!==(this._editVersion||0)||text!==String(this.data.guideJson||'').trim())return;apply();},fail:done,complete:done});}catch(e){done();this.setData({importError:'替换确认未完成，请重试'});}
 },
 async saveDraft(submit=false){
  await api.ensureLogin();
  if(this._owner===undefined)this._owner=this.ownerKey();if(!this.checkOwner())throw new Error('账号已切换，请重新打开我的模板');
  if(submit&&(this.data.importPending||this.data.importError))throw new Error('请先读取并确认AI回复，或清空粘贴区后手动填写');
  const body=sharing.content(this.data,this._tokens,submit);
  if(submit&&(!body.name||!body.prompt||body.cover_tokens.length<1||!this.data.consent))throw new Error('请填写名称、提示词、至少一张封面，并确认分享授权');
  const id=this.data.shareId;
  const r=await api.request(id?'/api/template-shares/'+encodeURIComponent(id):'/api/template-shares',{method:id?'PUT':'POST',data:id?{revision:this.data.revision,...body}:{request_id:this._requestId,...body}});
  if(!this.checkOwner())return null;const row=r.submission||r;this.applyShare(row);return row;
 },
 async onSaveDraft(){if(this.data.busy||this.data.locked||this.data.loading||!this.checkOwner())return;this.setData({busy:true,error:''});try{await this.saveDraft(false);if(!this._unloaded)wx.showToast({title:'草稿已保存',icon:'none'});}catch(e){if(!this._unloaded)this.setData({error:e.message||'保存失败'});}finally{if(!this._unloaded)this.setData({busy:false});}},
 async onSubmit(){if(this.data.busy||this.data.locked||this.data.loading||!this.checkOwner())return;this.setData({busy:true,error:''});try{await this.saveDraft(true);if(!this._unloaded)wx.showToast({title:'已提交，等待后台审核',icon:'none'});}catch(e){if(!this._unloaded)this.setData({error:(e.message||'提交暂未确认')+'；请在“我的模板”核对状态，不会因重试另建同一投稿。'});}finally{if(!this._unloaded)this.setData({busy:false});}},
 onChooseCover(){if(this.data.busy||this.data.locked||this.data.loading||!this.checkOwner())return;if(this.data.covers.length>=3){wx.showToast({title:'最多3张封面',icon:'none'});return;}wx.chooseMedia({count:3-this.data.covers.length,mediaType:['image'],sourceType:['album'],sizeType:['original','compressed'],success:r=>this.uploadChosen(r.tempFiles||[]),fail:e=>{if(!String(e.errMsg||'').includes('cancel'))wx.showToast({title:'选择封面失败',icon:'none'});}});},
 async uploadChosen(files){
  if(this.data.busy||this.data.locked||this.data.loading||!this.checkOwner())return;this.setData({busy:true,error:''});
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
