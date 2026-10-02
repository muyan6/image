/* User template submissions: metadata APIs and signed COS PUT only; no paid AI calls. */
export const GUIDE_PROMPT='请根据模板提示词及作者说明整理选图指南，返回 advice（整体建议）、suitable（适合照片）、unsuitable（不适合照片）、tips（上传前检查）四项。每组最多3条，每条不超过60字；不要编造已测试结论或保证效果，不输出模型、引擎、价格、尺寸等运行参数。信息不足时给出保守建议并提醒作者确认。';
export const TEMPLATE_SHARE_ROUTES={base:'/api/template-shares',mine:'/api/template-shares/mine',uploads:'/api/template-shares/uploads',item:id=>'/api/template-shares/'+encodeURIComponent(id),complete:token=>'/api/template-shares/uploads/'+encodeURIComponent(token)+'/complete',withdraw:id=>'/api/template-shares/'+encodeURIComponent(id)+'/withdraw'};
const owner=ctx=>ctx.state.user&&(ctx.state.user.account_user_id||ctx.state.user.user_id)||'';
const issue=(message,code='')=>Object.assign(new Error(message),{code});
const lines=value=>(Array.isArray(value)?value:String(value||'').split('\n')).map(x=>String(x).trim()).filter(Boolean).slice(0,8).map(x=>x.slice(0,60));
export function sharingContent(value){return {name:String(value.name||'').trim().slice(0,20),subtitle:String(value.subtitle||'').trim().slice(0,60),prompt:String(value.prompt||'').trim().slice(0,4000),guide:{advice:String(value.guide&&value.guide.advice||'').trim().slice(0,500),suitable:lines(value.guide&&value.guide.suitable),unsuitable:lines(value.guide&&value.guide.unsuitable),tips:lines(value.guide&&value.guide.tips)}};}
export function signedUploadUrl(value,base){try{const url=new URL(value);if(url.protocol!=='https:'||url.username||url.password||url.origin===new URL(base).origin)throw 0;return url.href;}catch(e){throw issue('封面直传地址异常，请重新取得上传凭据');}}
export function templateUploadHeaders(headers,type){const out={};for(const [key,value]of Object.entries(headers||{})){const name=key.toLowerCase();if(!['content-type','x-cos-acl'].includes(name)||typeof value!=='string'||/[\r\n]/.test(value)||out[name]&&out[name]!==value)throw issue('封面直传请求头异常，请重新取得上传凭据');out[name]=value;}if(out['content-type']&&out['content-type']!==type||out['x-cos-acl']&&out['x-cos-acl']!=='private')throw issue('封面上传凭据与图片格式不一致');return {'content-type':type,...out};}
export class TemplateSharingClient{
 constructor(ctx,options={}){this.ctx=ctx;this.version=ctx.accountVersion;this.owner=owner(ctx);this.closed=false;this.routes={...TEMPLATE_SHARE_ROUTES,...options.routes};this.document=options.document||ctx.document;this.storage=options.storage||this.document&&this.document.defaultView.localStorage;this.fetch=options.fetch||this.document&&this.document.defaultView.fetch&&this.document.defaultView.fetch.bind(this.document.defaultView)||globalThis.fetch.bind(globalThis);this.current=null;this.uploadController=null;this.key='image.web.template-share.v1:'+this.owner;}
 active(){return !this.closed&&this.owner&&this.ctx.accountVersion===this.version&&owner(this.ctx)===this.owner;}
 check(){if(!this.active())throw issue('页面或账号已变化，请重新打开我的模板','INACTIVE');}
 close(){this.closed=true;if(this.uploadController)this.uploadController.abort();}
 read(){try{return JSON.parse(this.storage&&this.storage.getItem(this.key)||'null');}catch(e){return null;}}
 persist(value){this.check();if(this.storage)this.storage.setItem(this.key,JSON.stringify(value));}
 begin(){this.check();if(this.storage)this.storage.removeItem(this.key);this.current=null;}
 async api(path,settings){this.check();if(!/^\/api\/template-shares(?:\/|\?|$)/.test(path))throw issue('模板接口地址异常');const data=await this.ctx.api(path,settings);this.check();return data;}
 accept(data){const row=data&&data.submission;if(!row||!row.id||!Number.isInteger(row.revision))throw issue('模板记录尚未确认，请到我的模板核对');this.current=row;this.persist({...this.read(),id:row.id,request_id:this.read()&&this.read().request_id});return row;}
 async load(id){return this.accept(await this.api(this.routes.item(id)));}
 async ensure(){if(this.current)return this.current;const saved=this.read();if(saved&&saved.id)return this.load(saved.id);const request_id=saved&&saved.request_id||'ts_'+globalThis.crypto.randomUUID().replace(/-/g,'');this.persist({request_id});return this.accept(await this.api(this.routes.base,{method:'POST',data:{request_id,submit:false}}));}
 async upload(file){
  this.check();if(!/^image\/(jpeg|png|webp)$/.test(file.type||'')||!file.size||file.size>8*1024*1024)throw issue('请选择8MB以内的 JPG、PNG 或 WebP 封面');
  const row=await this.ensure(),ext=file.type==='image/jpeg'?'jpg':file.type.split('/')[1];if(row.status==='pending')throw issue('审核中暂不可编辑，可撤回后修改');
  const ticket=await this.api(this.routes.uploads,{method:'POST',data:{share_id:row.id,revision:row.revision,ext,size:file.size,content_type:file.type}});
  if(typeof ticket.upload_token!=='string'||!ticket.upload_token)throw issue('封面上传凭据尚未确认，请重试');
  const url=signedUploadUrl(ticket.url,this.document.baseURI),Controller=this.document.defaultView.AbortController||globalThis.AbortController;this.uploadController=new Controller();
  try{const result=await this.fetch(url,{method:'PUT',credentials:'omit',headers:templateUploadHeaders(ticket.headers,file.type),body:file,signal:this.uploadController.signal});this.check();if(!result.ok)throw issue('封面直传未完成，请重新选择后重试');}finally{this.uploadController=null;}
  const completed=await this.api(this.routes.complete(ticket.upload_token),{method:'POST',data:{share_id:row.id,revision:row.revision}});this.accept(completed);if(!completed.cover||!completed.cover.token)throw issue('封面尚未确认，请核对后重试');return completed.cover;
 }
 async save(value,tokens,submit){const content=sharingContent(value);if(submit&&(!content.name||!content.prompt))throw issue('请填写模板名称和提示词');if(tokens.length>3||submit&&tokens.length<1)throw issue('提交审核需要1–3张已上传封面');const row=await this.ensure();if(row.status==='pending')throw issue('审核中暂不可编辑，可撤回后修改');return this.accept(await this.api(this.routes.item(row.id),{method:'PUT',data:{...content,revision:row.revision,cover_tokens:tokens,submit:!!submit,consent:!!submit}}));}
 async withdraw(){const row=this.current;if(!row)throw issue('缺少模板记录');return this.accept(await this.api(this.routes.withdraw(row.id),{method:'POST',data:{revision:row.revision}}));}
}
const LABELS={draft:'草稿',pending:'等待审核',rejected:'未通过',approved:'已通过',withdrawn:'已撤回'};
export async function mountTemplateSharing(ctx,root,route,params={}){
 let disposed=false,client=null;const version=ctx.accountVersion,valid=()=>!disposed&&version===ctx.accountVersion&&root.isConnected!==false;
 const n=(tag,cls='',text)=>ctx.element(tag,cls,text),button=(text,fn,cls='creation-button')=>{const b=n('button',cls,text);b.type='button';b.addEventListener('click',fn);return b;};
 const notice=n('p','creation-state');notice.setAttribute('role','status');const show=error=>{if(valid()&&error.code!=='INACTIVE'){notice.textContent=error.message||'操作暂未完成，请重试';ctx.toast(notice.textContent);}};
 const cleanup=()=>{disposed=true;if(client)client.close();};root.classList.add('creation-module','template-sharing');
 root.append(n('h1','',route==='template-shares'?'我的模板':'分享一个模板'),notice);
 if(!await ctx.requireLogin()||!valid())return cleanup;
 client=new TemplateSharingClient(ctx,ctx.templateSharingOptions||{});
 const run=fn=>async()=>{try{await fn();}catch(e){show(e);}};
 if(route==='template-shares'){
  root.append(button('新建模板',()=>ctx.navigate('template-share',{fresh:true})),button('返回模板页',()=>ctx.navigate('templates'),'creation-link-button'));
  const rewards=n('section','creation-note'),list=n('div','template-share-list'),more=button('加载更多',run(()=>load(true)),'creation-link-button');root.append(rewards,list,more);let loading=false,offset=0,hasMore=false,rows=[];
  async function load(append=false){if(loading||!valid())return;loading=true;more.disabled=true;notice.textContent='正在读取…';try{const data=await client.api(client.routes.mine+'?limit=24&offset='+(append?offset:0));if(!valid())return;if(!Array.isArray(data.items))throw issue('模板列表数据异常');const summary=data.rewards;rewards.replaceChildren();if(summary){rewards.append(n('p','','作者奖励 · 已产生 '+(summary.reward_earned??summary.earned??'—')+' 光子 · 已到账 '+(summary.reward_credited??summary.credited??'—')+' 光子'));if(Number(summary.recovery_due)>0)rewards.append(n('p','','待抵扣 '+summary.recovery_due+' 光子；仅从后续作者奖励抵扣，不扣已有余额。'));}rows=append?rows.concat(data.items):data.items;offset=Number(data.next_offset)||rows.length;hasMore=!!data.has_more;list.replaceChildren();for(const row of rows){const card=n('article','lib-card lib-card-body');card.append(n('h2','',row.name||'未命名模板'),n('p','lib-meta',LABELS[row.status]||row.status));if(row.reason)card.append(n('p','lib-error',row.reason));if(row.has_published)card.append(n('p','lib-meta','已审核版本仍在模板页展示；新版本通过后才替换'));
    card.append(button('查看 / 编辑',()=>ctx.navigate('template-share',{id:row.id}),'creation-link-button'));list.append(card);}notice.textContent=rows.length?'':'还没有分享模板';more.hidden=!hasMore;}catch(e){show(e);}finally{loading=false;more.disabled=false;}}
  root.append(button('刷新',run(()=>load(false)),'creation-link-button'));load();return cleanup;
 }
 let covers=[],files=[],busy=false;const form=n('form','template-share-form'),fields={};
 const field=(key,label,value='',multi=false,max=4000)=>{const control=n(multi?'textarea':'input','creation-input');control.name=key;control.value=value;control.maxLength=max;control.setAttribute('aria-label',label);const labelNode=n('label','creation-field');labelNode.append(n('span','creation-field-label',label),control);form.append(labelNode);fields[key]=control;return control;};
 const status=n('p','creation-note'),coverHost=n('div','template-share-covers');root.append(status,coverHost,form);
 field('name','模板名称（最多20字）','',false,20);field('subtitle','一句话介绍（可选）','',false,60);field('prompt','模板提示词','',true,4000);
 form.append(n('p','creation-note','填写风格、主体与画面要求即可；模型、运行档位和费用由平台确定。示例封面不会被当作输入照片。'));
 for(const [key,label]of [['advice','选图建议'],['suitable','适合这些照片（每行一条）'],['unsuitable','不太适合（每行一条）'],['tips','上传前这样选（每行一条）']])field(key,label,'',true,key==='advice'?500:500);
 const aiPrompt=field('ai_prompt','AI整理指南参考指令',GUIDE_PROMPT,true,2000);aiPrompt.readOnly=true;
 form.append(button('复制AI整理指令',run(async()=>{const clipboard=root.ownerDocument.defaultView.navigator.clipboard;if(clipboard){await clipboard.writeText(GUIDE_PROMPT+'\n模板提示词：'+fields.prompt.value);notice.textContent='指令已复制，请自行整理后填入四项指南';}else{aiPrompt.focus();aiPrompt.select();notice.textContent='已选中指令，可直接复制';}}),'creation-link-button'));
 const picker=field('files','上传1–3张封面');picker.type='file';picker.accept='image/jpeg,image/png,image/webp';picker.multiple=true;
 const consent=n('input');consent.type='checkbox';consent.name='consent';const consentLabel=n('label','creation-field');consentLabel.append(consent,n('span','','我确认可以分享封面与提示词，同意审核后公开展示模板；提示词仅供平台生成使用。'));form.append(n('p','creation-note','未提交审核的草稿和封面仅本人及后台可见；保存草稿、上传封面无需勾选公开授权。'),consentLabel);
 picker.addEventListener('change',()=>{files=Array.from(picker.files||[]);if(covers.length+files.length>3){notice.textContent='封面合计最多3张，请移除或重新选择';files=[];picker.value='';}else notice.textContent=files.length?'待上传 '+files.length+' 张封面':'';});
 const pending=()=>client.current&&client.current.status==='pending';
 function lockControls(){form.querySelectorAll('input,textarea,button').forEach(node=>node.disabled=busy||!!pending());coverHost.querySelectorAll('button').forEach(node=>node.disabled=busy||!!pending());}
 function drawCovers(){coverHost.replaceChildren();covers.forEach((cover,index)=>{const box=n('div','template-share-cover'),img=n('img');let url;try{url=new URL(cover.thumbnail_url||cover.preview_url);if(url.protocol!=='https:')throw 0;}catch(e){return;}img.src=url.href;img.alt='模板封面 '+(index+1);img.loading='lazy';box.append(img,button('移除封面',()=>{if(!busy&&!pending()){covers.splice(index,1);drawCovers();}},'creation-link-button'));coverHost.append(box);});lockControls();}
 function fill(row){if(!valid())return;consent.checked=false;for(const key of ['name','subtitle','prompt'])fields[key].value=row[key]||'';for(const key of ['advice','suitable','unsuitable','tips'])fields[key].value=Array.isArray(row.guide&&row.guide[key])?row.guide[key].join('\n'):row.guide&&row.guide[key]||'';covers=Array.isArray(row.covers)?row.covers:[];status.textContent=(LABELS[row.status]||'新模板')+(row.status==='pending'?' · 审核中暂不可编辑，可撤回后修改':'')+(row.has_published?' · 修改版审核期间，上一版继续展示':'')+(row.reason?' · '+row.reason:'');drawCovers();}
 const read=()=>({name:fields.name.value,subtitle:fields.subtitle.value,prompt:fields.prompt.value,guide:Object.fromEntries(['advice','suitable','unsuitable','tips'].map(key=>[key,fields[key].value]))});
 async function save(submit){if(busy||pending())return;busy=true;lockControls();try{const values=read();if(submit&&!consent.checked)throw issue('请先确认本次分享授权');if(submit&&(!values.name.trim()||!values.prompt.trim()))throw issue('请填写模板名称和提示词');if(covers.length+files.length>3)throw issue('封面合计最多3张');while(files.length){notice.textContent='正在直传封面…';const cover=await client.upload(files[0]);if(!valid())return;covers.push(cover);files.shift();drawCovers();}files=[];picker.value='';const row=await client.save(values,covers.map(cover=>cover.token),submit);if(!valid())return;fill(row);notice.textContent=submit?'已提交审核；审核结果可在我的模板查看':'草稿已保存';}finally{busy=false;if(valid())lockControls();}}
 form.append(button('保存草稿',run(()=>save(false)),'creation-link-button'),button('提交审核',run(()=>save(true))));form.addEventListener('submit',event=>{event.preventDefault();run(()=>save(true))();});
 root.append(button('我的模板',()=>ctx.navigate('template-shares'),'creation-link-button'),button('撤回此模板',run(async()=>{if(busy||!client.current)return;if(!await ctx.confirm('撤回后模板停止公开展示，确定撤回？'))return;busy=true;lockControls();try{fill(await client.withdraw());notice.textContent='模板已撤回';}finally{busy=false;if(valid())lockControls();}}),'creation-link-button'));
 (async()=>{try{if(params.fresh)client.begin();const id=params.id||(client.read()||{}).id;if(id){busy=true;lockControls();status.textContent='正在读取模板版本…';fill(await client.load(id));}}catch(e){show(e);}finally{busy=false;if(valid())lockControls();}})();return cleanup;
}
