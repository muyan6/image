/* Creation parity. Business requests use ctx.api; image bytes go directly to COS. */
import {createVisibilityGate,mediaReusable,reuseDisplayMedia,reconcileById,armReadTimeout} from './shared-load.js';
const STAGES={queued:'云端排队中',normalize:'正在准备照片',enhance:'正在生成画面',finalize:'正在整理成品',store_cos:'正在保存成品',submission_unknown:'正在确认生成状态'};
const PHOTO_RATIOS=['original','1:1','3:4','4:3','9:16','16:9'];
const TEXT_RATIOS=['1:1','3:2','2:3'];
const EXAMPLES=[['水彩风景','雨后的森林小屋，暖色灯光，水彩插画风格。'],['简约壁纸','暖白色背景，一枝浅绿色植物，柔和自然光，简约手机壁纸。'],['旅行插画','海边小镇的午后，蓝白房屋与橘色屋顶，清新旅行插画。']];
function problem(message,status=0,code=''){return Object.assign(new Error(message),{status,code});}
function account(ctx){return ctx.state.user&&(ctx.state.user.account_user_id||ctx.state.user.user_id)||'';}
function complete(recipe){return !!(recipe&&(recipe.complete===true||recipe.recipe_complete===true));}
function json(value){return JSON.parse(JSON.stringify(value));}
function price(config,mode,quality='light'){
  const value=mode==='text'?config&&config.text_generation&&config.text_generation.price:config&&config.prices&&config.prices[quality];
  if(!Number.isInteger(value)||value<0)throw problem('生成价格尚未加载，请刷新后重试');
  return config.free_mode?0:value;
}
function freshDraft(mode='photo') {return {input_mode:mode,prompt:'',quality:'light',template_id:'',text_fields:{},custom_prompt:'',show_custom_prompt:false,aspect_ratio:mode==='text'?'1:1':'original',template_output_mode:'template',blob:null};}
function directMedia(url){if(!/^https:\/\//.test(url||'')||new URL(url).origin===globalThis.location.origin)throw problem('图片尚未提供 COS 直链，请稍后重新打开作品');return url;}
async function imageBlob(blob){
  if(/^image\//.test(blob.type||''))return blob;
  const bytes=new Uint8Array(await blob.slice(0,16).arrayBuffer());let type='';
  if(bytes[0]===255&&bytes[1]===216&&bytes[2]===255)type='image/jpeg';
  else if(bytes[0]===137&&bytes[1]===80&&bytes[2]===78&&bytes[3]===71)type='image/png';
  else if(bytes[0]===82&&bytes[1]===73&&bytes[2]===70&&bytes[3]===70&&bytes[8]===87&&bytes[9]===69&&bytes[10]===66&&bytes[11]===80)type='image/webp';
  if(!type)throw problem('图片数据异常，请重新选择照片或重试下载');return new Blob([blob],{type});
}

export function createReceiptStore(storage){
  const key=owner=>'image.web.submission.v1:'+owner;
  const local=()=>{try{return storage||globalThis.localStorage;}catch(e){throw problem('提交回执暂不可读，请恢复浏览器存储后再生成');}};
  return {
    read(owner){if(!owner)return null;let value;try{const raw=local().getItem(key(owner));if(!raw)return null;value=JSON.parse(raw);}catch(e){throw problem('提交回执暂不可读，请恢复浏览器存储后再生成');}
      if(value.owner!==owner||!/^wc_[A-Za-z0-9_-]{8,70}$/.test(value.client_request_id||'')||!['photo','text'].includes(value.input_mode)||!value.payload)throw problem('提交回执不完整，请先在作品中核对上次提交');return value;},
    write(owner,value){if(!owner)throw problem('请先登录再生成',401);const saved=Object.assign({},value,{owner});const raw=JSON.stringify(saved);
      try{local().setItem(key(owner),raw);if(local().getItem(key(owner))!==raw)throw new Error('not durable');}catch(e){throw problem('浏览器存储空间不足，提交回执未保存；本次不会开始付费生成');}return saved;},
    clear(owner,id){const old=this.read(owner);if(old&&old.client_request_id!==id)return;try{local().removeItem(key(owner));if(local().getItem(key(owner)))throw new Error('not removed');}catch(e){throw problem('提交回执清理未完成，请稍后重试');}}
  };
}
export function createDraftStore(factory){
  let opening;
  const open=()=>opening||(opening=new Promise((resolve,reject)=>{
    try{factory=factory||globalThis.indexedDB;}catch(e){}
    if(!factory){reject(problem('此浏览器暂不支持照片草稿保存，请换用支持本地存储的浏览器'));return;}
    const request=factory.open('image-web-creation-v1',1);
    request.onupgradeneeded=()=>{if(!request.result.objectStoreNames.contains('drafts'))request.result.createObjectStore('drafts');};
    request.onsuccess=()=>resolve(request.result);request.onerror=()=>{opening=null;reject(problem('草稿存储打开失败，请检查浏览器存储设置'));};
    request.onblocked=()=>{opening=null;reject(problem('草稿存储正在更新，请关闭旧页面后重试'));};
  }));
  async function transaction(mode,action){const db=await open();return new Promise((resolve,reject)=>{
    const tx=db.transaction('drafts',mode);let result;const request=action(tx.objectStore('drafts'));
    request.onsuccess=()=>{result=request.result;};tx.oncomplete=()=>resolve(result);
    tx.onerror=tx.onabort=()=>reject(problem('草稿保存未完成，可能是浏览器空间不足；参数仍保留在当前页'));
  });}
  return {async read(owner){const value=await transaction('readonly',store=>store.get('latest'));return value&&value.owner===owner?value.draft:null;},
    async write(owner,draft){if(!owner)throw problem('登录后才会保存个人草稿',401);await transaction('readwrite',store=>store.put({owner,draft,updated_at:Date.now()},'latest'));return draft;},
    async clear(owner){const value=await transaction('readonly',store=>store.get('latest'));if(value&&value.owner===owner)await transaction('readwrite',store=>store.delete('latest'));}};
}

export class CreationSession {
  constructor(ctx,options={}){
    this.ctx=ctx;this.version=ctx.accountVersion;this.owner=account(ctx);this.closed=false;this.busy=false;this.templates=[];this.groups=[];
    this.config=ctx.state.config||null;this.preferences={template_favorites:[],recent_templates:[]};
    this.receipts=options.receipts||createReceiptStore();this.drafts=options.drafts||createDraftStore();
    this.fetch=options.fetch||globalThis.fetch.bind(globalThis);this.delay=options.delay||(ms=>new Promise(r=>{this.timer=setTimeout(r,ms);this.timerResolve=r;}));
    this.document=options.document||ctx.document||globalThis.document;this.visibility=createVisibilityGate(this.document,()=>this.active());
    this.visibility.subscribe(()=>{if(this.timer){clearTimeout(this.timer);this.timerResolve&&this.timerResolve();}if(!this.visibility.visible()&&this.pollController)this.pollController.abort();});
  }
  active(){return !this.closed&&this.ctx.accountVersion===this.version;}
  assertActive(){if(!this.active())throw problem('页面或登录状态已变化，请在当前页面继续',0,'INACTIVE');}
  close(){this.closed=true;this.visibility.close();if(this.pollController)this.pollController.abort();if(this.timer){clearTimeout(this.timer);this.timerResolve&&this.timerResolve();}}
  async login(){const ok=await this.ctx.requireLogin();if(!ok)return false;this.assertActive();this.owner=account(this.ctx);if(!this.owner)throw problem('登录信息尚未就绪，请重新登录',401);return true;}
  async catalog(targetId='',force=false){
    const data=await (this.ctx.displayGet?this.ctx.displayGet('/api/templates',{public:true,force}):this.ctx.api('/api/templates'));this.assertActive();
    const old=new Map(this.templates.map(item=>[item.id,item]));this.templates=(Array.isArray(data.items)?data.items:[]).map(item=>reuseDisplayMedia(item,old.get(item.id)));this.groups=Array.isArray(data.groups)?data.groups:[];
    const target=targetId?this.templates.find(t=>t.id===targetId):null;
    if(targetId&&!target){if(!force&&this.ctx.displayGet)return this.catalog(targetId,true);throw problem('所选风格已下架或暂不可用，请重试或主动选择其他风格',404,'TEMPLATE_MISSING');}
    return target;
  }
  async loadPreferences(){if(!account(this.ctx))return this.preferences;const value=await (this.ctx.displayGet?this.ctx.displayGet('/api/me/preferences'):this.ctx.api('/api/me/preferences'));this.assertActive();
    this.preferences={template_favorites:value.template_favorites||[],recent_templates:value.recent_templates||[]};return this.preferences;}
  filter(query='',category='all',scope='all',order='hot',ranks=new Map()){
    const words=query.trim().toLowerCase().split(/\s+/).filter(Boolean),prefs=this.preferences;
    return this.templates.filter(t=>(category==='all'||t.group_id===category)&&(scope!=='favorites'||prefs.template_favorites.includes(t.id))&&(scope!=='recent'||prefs.recent_templates.includes(t.id))&&words.every(w=>[t.name,t.subtitle,t.group_name,...(Array.isArray(t.tags)?t.tags:[])].join(' ').toLowerCase().includes(w)))
      .sort((a,b)=>scope==='recent'?prefs.recent_templates.indexOf(a.id)-prefs.recent_templates.indexOf(b.id):category!=='all'?(Number(a.sort)||0)-(Number(b.sort)||0):order==='latest'?(Number(b.published_at)||0)-(Number(a.published_at)||0):order==='random'?(ranks.get(a.id)||0)-(ranks.get(b.id)||0):(Number(b.usage_count)||0)-(Number(a.usage_count)||0));
  }
  async favorite(id){if(!await this.login())return;const favorite=!this.preferences.template_favorites.includes(id);const value=await this.ctx.api('/api/me/preferences',{method:'PUT',data:{template_id:id,favorite}});this.assertActive();this.preferences=value;return value;}
  async saveDraft(draft){this.assertActive();if(!this.owner)throw problem('登录后才会保存个人草稿',401);await this.drafts.write(this.owner,draft);this.assertActive();}
  async readDraft(){if(!this.owner)return null;const draft=await this.drafts.read(this.owner);this.assertActive();return draft;}
  async original(recipe){
    if(!recipe.orig_url)throw problem('原图已到保存期限，请重新选择照片',410);
    let url=recipe.orig_url;
    for(let attempt=0;attempt<2;attempt++){
      const response=await this.fetch(directMedia(url),{credentials:'omit'});this.assertActive();
      if(response.ok){const blob=await imageBlob(await response.blob());this.assertActive();return blob;}
      if(attempt===0&&[401,403].includes(response.status)&&recipe.job_id){const refreshed=await this.ctx.api('/api/jobs/'+encodeURIComponent(recipe.job_id)+'/recipe');this.assertActive();if(!complete(refreshed)||!refreshed.orig_url)throw problem('原素材已到保存期限，请重新选择照片',410);url=refreshed.orig_url;continue;}
      throw problem(response.status===404?'原图已到保存期限，请重新选择照片':'原图下载失败，请稍后重试',response.status);
    }
  }
  async resultMedia(jobId,kind,url){
    this.assertActive();if(!['result','orig'].includes(kind)||!jobId||!url)throw problem('图片已到期或尚未就绪，请重新打开作品');
    let parsed;try{parsed=new URL(url,globalThis.location.origin);}catch(e){throw problem('图片地址异常，请重新打开作品');}
    if(parsed.origin===globalThis.location.origin&&/^\/api\/images\//.test(parsed.pathname)){
      const repaired=await this.ctx.api('/api/jobs/'+encodeURIComponent(jobId)+'/refresh-media?kind='+kind,{method:'POST',data:{}});this.assertActive();
      return directMedia(repaired&&repaired.url);
    }
    return directMedia(url);
  }
  async recipe(value){
    await this.newCreation();
    const recipe=typeof value==='string'?await this.ctx.api('/api/jobs/'+encodeURIComponent(value)+'/recipe'):value;this.assertActive();
    if(!complete(recipe))throw problem('旧作品的创作参数不完整，请重新选图或填写描述');
    const draft=Object.assign(freshDraft(recipe.input_mode),recipe,{rights:false,show_custom_prompt:!!recipe.custom_prompt});
    if(draft.input_mode==='text'){if(!(draft.prompt||'').trim())throw problem('旧作品未保存描述，请重新填写');}
    else draft.blob=await this.original(recipe);
    await this.saveDraft(draft);return draft;
  }
  receipt(){return this.receipts.read(this.owner);}
  async lookup(){
    const receipt=this.receipt();if(!receipt)return null;
    try{
      const found=await this.ctx.api('/api/me/submissions/'+encodeURIComponent(receipt.client_request_id));this.assertActive();
      if(found.state==='accepted'&&found.job_id){this.accept(found,receipt);return found;}
      if(found.state==='rejected'){this.receipts.clear(this.owner,receipt.client_request_id);return found;}
      return Object.assign({},found,{receipt});
    }catch(e){if(e.status===404){this.assertActive();return {state:'not_found',receipt};}throw e;}
  }
  accept(created,receipt){
    this.assertActive();
    try{this.receipts.write(this.owner,Object.assign({},receipt,{state:'accepted',job_id:created.job_id,status:created.status||'processing'}));}catch(e){this.ctx.toast('任务已受理；旧提交编号仍保留，稍后可在作品中找回');}
    if(typeof created.balance==='number')this.ctx.setBalance(created.balance);
    this.ctx.invalidateAccount();
    if(receipt.input_mode==='photo'&&receipt.payload.template_id)this.ctx.api('/api/me/recent-template',{method:'POST',data:{template_id:receipt.payload.template_id}}).catch(()=>{});
  }
  async freshPrice(mode,quality,displayed){
    const [config,me]=await Promise.all([this.ctx.api('/api/config'),this.ctx.api('/api/me')]);this.assertActive();
    this.config=config;this.ctx.state.config=config;if(typeof me.balance==='number')this.ctx.setBalance(me.balance);
    const cost=price(config,mode,quality);
    if(cost!==displayed)throw problem('生成价格已更新，请确认新的价格后再次点击生成',409,'PRICE_CHANGED');
    if((typeof me.balance==='number'?me.balance:0)<cost)throw problem('光子余额不足，请联系管理员补充额度',402);
    return cost;
  }
  async submit(draft,displayedPrice,{retry=false}={}){
    if(this.busy)throw problem('提交正在进行，请稍候');this.busy=true;let receipt,attempted=false,reused=false;
    try {
      if(!await this.login())return null;
      receipt=this.receipt();reused=!!receipt;
      if(receipt){
        if(receipt.state==='accepted'&&receipt.job_id)return {job_id:receipt.job_id,status:receipt.status,recovered:true};
        const found=await this.lookup();
        if(found&&found.state==='accepted')return found;
        if(found&&found.state==='rejected'){receipt=null;reused=false;}
        else if(!retry||!found||found.state!=='not_found')throw problem('上次提交仍待确认，请先核对；本次不会再次扣费',0,'UNCERTAIN');
      }
      const mode=receipt?receipt.input_mode:draft.input_mode;
      const source=receipt?Object.assign({},draft,receipt.draft,{blob:draft.blob||receipt.draft.blob}):draft;
      const cost=await this.freshPrice(mode,source.quality,displayedPrice);
      if(mode==='text'){
        if(!this.config.text_generation||!this.config.text_generation.ready)throw problem('文字生图暂未开放');
        if(!(source.prompt||'').trim())throw problem('请描述想生成的画面');
      }else{
        if(!source.blob||!/^image\//.test(source.blob.type||''))throw problem('请先选择可用的照片');
        if(!draft.rights)throw problem('请先确认照片使用权');
        if(source.template_id&&!this.templates.some(t=>t.id===source.template_id))throw problem('所选风格尚未确认，重试成功前不会生成');
        if(source.template_id)await this.catalog(source.template_id,true);
        if(source.template_id&&(!Array.isArray(this.config.template_quality_options)||!this.config.template_quality_options.includes(source.quality)))throw problem('当前生成档位尚未开放');
        if(source.template_id&&source.template_output_mode==='single'&&!(this.config.template_output_modes||[]).includes('single'))throw problem('单图模式尚未开放');
        if(!this.config.cos_ready)throw problem('照片上传通道尚未就绪，请稍后重试');
      }
      const stored=Object.assign({},source,{rights:false});delete stored.orig_url;
      await this.saveDraft(stored);
      let payload=receipt?Object.assign({},receipt.payload):mode==='text'?{prompt:source.prompt.trim(),aspect_ratio:source.aspect_ratio}:{quality:source.quality,template_id:source.template_id||'',text_fields:JSON.stringify(source.text_fields||{}),template_output_mode:source.template_id?source.template_output_mode:'template',aspect_ratio:source.template_id?'':source.aspect_ratio,custom_prompt:source.template_id?'':(source.show_custom_prompt?source.custom_prompt.trim():'')};
      payload.expected_price=cost;
      if(!receipt)receipt={client_request_id:'wc_'+Date.now().toString(36)+'_'+globalThis.crypto.randomUUID().replace(/-/g,''),input_mode:mode,payload,draft:Object.assign({},stored,{blob:null}),state:'preparing',created_at:Date.now()};
      receipt=this.receipts.write(this.owner,Object.assign({},receipt,{payload,state:'preparing'}));
      if(mode==='photo'){
        const ticket=await this.ctx.api('/api/uploads',{method:'POST',data:{filename:'photo.'+(source.blob.type==='image/png'?'png':'jpg'),byte_size:source.blob.size}});this.assertActive();
        if(!/^https:\/\//.test(ticket.url||'')||new URL(ticket.url).origin===globalThis.location.origin)throw problem('COS 上传地址异常，请稍后重试');
        const uploaded=await this.fetch(ticket.url,{method:'PUT',body:source.blob,credentials:'omit',headers:{'content-type':'application/octet-stream'}});this.assertActive();
        if(!uploaded.ok)throw problem('照片直传未完成，请重试',uploaded.status);
        await this.ctx.api('/api/uploads/'+encodeURIComponent(ticket.upload_id)+'/complete',{method:'POST',data:{}});this.assertActive();payload=Object.assign({},payload,{upload_id:ticket.upload_id});
      }
      receipt=this.receipts.write(this.owner,Object.assign({},receipt,{state:'submitting'}));attempted=true;
      const created=await this.ctx.api(mode==='text'?'/api/text-generation':'/api/rescue/by-upload',{method:'POST',data:Object.assign({},payload,{client_request_id:receipt.client_request_id})});this.assertActive();
      if(!created||!created.job_id)throw problem('提交响应尚未确认，请保留提交编号',0,'UNCERTAIN');
      this.accept(created,receipt);return created;
    }catch(e){
      if(e.code==='INACTIVE')throw e;
      if(receipt&&(attempted||reused)){
        if(!reused&&attempted&&e.status>=400&&e.status<500&&e.status!==409){this.receipts.clear(this.owner,receipt.client_request_id);throw e;}
        try{this.receipts.write(this.owner,Object.assign({},receipt,{state:attempted?'uncertain':receipt.state}));}catch(ignore){}
        if(attempted||reused){
          let found;try{found=await this.lookup();}catch(lookupError){if(lookupError.code==='INACTIVE')throw lookupError;}
          if(found&&found.state==='accepted')return found;if(found&&found.state==='rejected')throw problem(found.detail||e.message,found.http_status||e.status,'SUBMISSION_REJECTED');
          throw problem(e.code==='PRICE_CHANGED'?e.message:'提交结果仍待确认，请先核对；不会自动再次扣费',e.status,e.code==='PRICE_CHANGED'?e.code:'UNCERTAIN');
        }
      }
      if(receipt&&!attempted&&!reused)this.receipts.clear(this.owner,receipt.client_request_id);
      throw e;
    }finally{this.busy=false;}
  }
  async poll(jobId,onTick){
    if(this.polling)return;this.polling=true;const started=Date.now(),hardUntil=started+30*60*1000;let until=started+180000,remaining=180000,paused=!this.visibility.visible(),last='',failures=0;
    // Hidden time does not consume the three-minute foreground budget; absolute lifetime is bounded.
    const unsubscribe=this.visibility.subscribe(visible=>{if(visible&&paused){until=Math.min(hardUntil,Date.now()+remaining);paused=false;}else if(!visible&&!paused){remaining=Math.max(0,until-Date.now());paused=true;}});
    try{while(this.active()&&Date.now()<hardUntil&&(paused||Date.now()<until)){if(!this.visibility.visible())await this.visibility.wait();this.assertActive();if(Date.now()>=until||Date.now()>=hardUntil)return null;
      let job,cancelRead=()=>{};try{const Controller=this.document&&this.document.defaultView&&this.document.defaultView.AbortController||globalThis.AbortController;this.pollController=Controller?new Controller():null;cancelRead=armReadTimeout(this.pollController,Math.min(until,hardUntil));job=await this.ctx.api('/api/jobs/'+encodeURIComponent(jobId),this.pollController?{signal:this.pollController.signal}:{});this.assertActive();failures=0;
        if(!this.visibility.visible())continue;const signature=job.status+':'+job.stage;if(signature!==last){last=signature;onTick(job);}if(job.status==='succeeded'||job.status==='failed')return job;
      }catch(e){this.assertActive();if(!this.visibility.visible())continue;if(e.status>=400&&e.status<500&&e.status!==429)throw e;failures++;}
      finally{cancelRead();this.pollController=null;}
      await this.delay(Math.min(failures?Math.min(15000,1500*2**Math.min(failures,4)):Date.now()-started<60000?1500:5000,Math.max(0,until-Date.now())));
    }return null;}finally{unsubscribe();this.polling=false;}
  }
  async newCreation(){this.assertActive();const receipt=this.receipt();if(receipt&&!(receipt.state==='accepted'&&receipt.job_id))throw problem('上次提交仍待确认，请先核对');if(receipt)this.receipts.clear(this.owner,receipt.client_request_id);}
}

export async function mountCreation(ctx,root,route,params={}) {
  const session=new CreationSession(ctx,ctx.creationOptions||{});const urls=new Set();let draft=freshDraft(route==='text'?'text':'photo'),target=null,templateError='',status='',formBusy=false,knownJob='',retryable=false,disposed=false;
  const active=()=>session.active()&&!disposed;
  const node=(tag,cls='',text='')=>ctx.element(tag,cls,text);
  function add(parent,...children){children.filter(Boolean).forEach(child=>parent.append(child));return parent;}
  function button(text,fn,cls='creation-button'){const b=node('button',cls,text);b.type='button';b.addEventListener('click',fn);return b;}
  function note(text){return node('p','creation-note',text);}
  function error(e){if(active()&&e.code!=='INACTIVE'){status=e.message||'操作未完成，请稍后重试';ctx.toast(status);if(route==='result')root.replaceChildren(note(status),button('返回作品',()=>ctx.navigate('works')));else render();}}
  function run(fn){return async()=>{try{await fn();}catch(e){error(e);}};}
  function field(title,input,hint=''){const label=node('label','creation-field');if(input.type==='checkbox')label.classList.add('creation-checkbox-field');add(label,node('span','creation-field-label',title),input,hint&&note(hint));return label;}
  function select(values,current,onChange){const el=node('select','creation-input');values.forEach(value=>{const option=node('option','',value==='original'?'保持原图':value);option.value=value;option.selected=value===current;el.append(option);});el.addEventListener('change',()=>{onChange(el.value);scheduleDraft();});return el;}
  function input(value,onInput,multiline=false,max=500,save=true){const el=node(multiline?'textarea':'input','creation-input');el.value=value||'';el.maxLength=max;if(multiline)el.rows=5;else el.type='text';el.addEventListener('input',()=>{onInput(el.value);if(save)scheduleDraft();});return el;}
  let saveTimer,dirty=false;
  function scheduleDraft(){dirty=true;if(!session.owner)return;clearTimeout(saveTimer);saveTimer=setTimeout(()=>{if(active())session.saveDraft(Object.assign({},draft,{rights:false})).catch(e=>{if(active()){status=e.message;ctx.toast(status);}});},300);}
  async function login(){const ok=await ctx.requireLogin();if(!ok||disposed||session.closed||root.isConnected===false)return false;
    if(ctx.accountVersion!==session.version){ctx.navigate(route,Object.assign({},params,{initialDraft:draft}));return false;}
    session.owner=account(ctx);return active()&&!!session.owner;
  }
  function money(mode=draft.input_mode){try{return price(session.config,mode,draft.quality);}catch(e){return null;}}
  function templateCard(item){
    const card=node('article','creation-template-card'),img=node('img','creation-template-cover');img.src=item.thumbnail||item.thumbnailUrl||item.cover||(item.covers||[])[0]||'/favicon.svg';img.alt=item.name||'风格示例';img.loading='lazy';
    const media=node('div','creation-template-media'),preview=button('',()=>detail(item),'creation-template-preview');preview.setAttribute('aria-label','查看 '+(item.name||'模板')+' 的风格详情');preview.append(img,node('span','creation-source-badge',item.source==='user'?'用户分享':'官方'));media.append(preview);card.append(media);
    let repair=0;img.addEventListener('error',run(async()=>{if(!active())return;if(repair++===0){try{await session.catalog('',true);if(!active())return;const fresh=session.templates.find(t=>t.id===item.id);if(!fresh){card.remove();return;}Object.assign(item,fresh);const url=fresh.thumbnail||fresh.thumbnailUrl;if(url&&url!==img.src&&mediaReusable(url)){img.src=url;return;}}catch(e){if(!active())return;img.alt='风格图片信息读取失败';ctx.toast(e.message||'风格图片信息读取失败，请刷新');return;}}if(repair<=2){const full=item.cover||(item.covers||[])[0];if(full&&mediaReusable(full)){img.src=full;return;}}img.alt='风格图片暂不可用';}));
    const body=node('div','creation-template-body'),titleRow=node('div','creation-template-title-row'),title=node('h3','creation-template-name',item.name);
    title.title=item.name||'';const favored=session.preferences.template_favorites.includes(item.id);
    const favorite=button(favored?'★':'☆',run(async()=>{if(!await login())return;await session.favorite(item.id);renderTemplates();}),'creation-link-button creation-favorite');
    favorite.setAttribute('aria-label',(favored?'取消收藏':'收藏')+' '+(item.name||'模板'));favorite.setAttribute('aria-pressed',String(favored));favorite.title=favored?'取消收藏':'收藏模板';titleRow.append(title);media.append(favorite);
    const footer=node('div','creation-template-footer');add(footer,node('span','creation-template-usage',(item.usage_count||0)+' 次创作'),button('了解风格',()=>detail(item),'creation-link-button creation-template-open'));
    add(body,titleRow,footer);card.append(body);
    return card;
  }
  let category='all',scope='all',search='',order='hot';const randomRanks=new Map();
  function renderTemplates(){if(!active())return;if(category!=='all'&&!session.groups.some(g=>g.id===category))category='all';const existingGrid=root.querySelector('.creation-template-grid');root.replaceChildren();root.classList.add('creation-catalog');const head=node('header','creation-heading');add(head,node('p','creation-eyebrow','废片新生所 · 风格图鉴'),node('h1','','选一个喜欢的风格'),note('先看示例与选图建议，再开始创作。'));root.append(head);
    const query=input(search,value=>{search=value;renderTemplateGrid();},false,40,false);query.classList.add('creation-catalog-search');query.placeholder='搜索模板名称、风格或关键词';query.setAttribute('aria-label','搜索风格');root.append(query);
    const tabs=node('nav','creation-tabs creation-catalog-scope');tabs.setAttribute('aria-label','模板范围');[['all','全部模板'],['favorites','我的收藏'],['recent','最近使用']].forEach(([id,name])=>tabs.append(button(name,run(async()=>{if(id!=='all'&&!await login())return;scope=id;renderTemplates();}),'creation-tab'+(scope===id?' is-active':''))));root.append(tabs);
    if(scope==='all'){
      const filters=node('div','creation-catalog-filters'),cats=node('select','creation-input');cats.setAttribute('aria-label','模板分类');
      [{id:'all',name:'全部分类'},...session.groups].forEach(group=>{const option=node('option','',group.name);option.value=group.id;option.selected=category===group.id;cats.append(option);});cats.addEventListener('change',()=>{category=cats.value;renderTemplates();});
      const sorting=node('select','creation-input'),shuffle=button('换一批',()=>{randomRanks.clear();session.templates.forEach(item=>randomRanks.set(item.id,Math.random()));renderTemplateGrid();},'creation-tab creation-catalog-shuffle');shuffle.hidden=category!=='all'||order!=='random';sorting.setAttribute('aria-label','模板排序');sorting.disabled=category!=='all';
      for(const [value,label]of [['hot','按热度'],['latest','按最新'],['random','随机浏览']]){const option=node('option','',category!=='all'&&order===value?'分类顺序':label);option.value=value;option.selected=order===value;sorting.append(option);}
      sorting.addEventListener('change',()=>{order=sorting.value;shuffle.hidden=order!=='random';if(order==='random'){randomRanks.clear();session.templates.forEach(item=>randomRanks.set(item.id,Math.random()));}renderTemplateGrid();});add(filters,cats,sorting,shuffle);root.append(filters);
    }
    if(templateError)add(root,note(templateError),button('重新加载风格',run(()=>loadCatalog(true))));
    if(status)root.append(note(status));root.append(existingGrid||node('div','creation-template-grid'));renderTemplateGrid();
  }
  function renderTemplateGrid(){if(!active())return;const grid=root.querySelector('.creation-template-grid');if(!grid)return;if(order==='random')session.templates.forEach(item=>{if(!randomRanks.has(item.id))randomRanks.set(item.id,Math.random());});const filtered=session.filter(search,scope==='all'?category:'all',scope,order,randomRanks);if(scope==='favorites')filtered.sort((a,b)=>session.preferences.template_favorites.indexOf(a.id)-session.preferences.template_favorites.indexOf(b.id));const items=filtered.map(item=>({...item,display_favorite:session.preferences.template_favorites.includes(item.id)}));reconcileById(grid,items,templateCard);if(!items.length)grid.append(node('p','creation-note creation-catalog-empty',templateError?'加载后再浏览风格':'没有找到符合条件的风格，试试其他关键词或分类。'));}
  function detail(item){
    if(!active())return;
    if((Array.isArray(item.covers)?item.covers:[item.cover]).filter(Boolean).some(url=>!mediaReusable(url))){run(async()=>{await session.catalog(item.id,true);const fresh=session.templates.find(t=>t.id===item.id);if(fresh&&(Array.isArray(fresh.covers)?fresh.covers:[fresh.cover]).filter(Boolean).every(url=>mediaReusable(url)))detail(fresh);else throw problem('风格图片签名尚未更新，请刷新后重试');})();return;}
    const dialog=node('dialog','creation-dialog'),content=node('section','creation-template-detail');
    const heading=node('header','creation-detail-heading'),headingTitle=node('h2','',item.name);headingTitle.tabIndex=-1;add(heading,headingTitle,button('关闭',()=>dialog.close(),'creation-dialog-close'));content.append(heading);
    const covers=(Array.isArray(item.covers)&&item.covers.length?item.covers:[item.cover]).filter(Boolean);covers.forEach((url,i)=>{const img=node('img','creation-detail-image');img.src=url;img.alt=(item.name||'风格')+'示例 '+(i+1);content.append(img);});
    add(content,note(item.subtitle||''),note(item.guide&&item.guide.advice||'请选择主体清楚、遮挡较少的照片。'));
    if(item.source==='user'&&item.author_name)content.append(note('分享者：'+item.author_name));
    for(const [key,title]of [['suitable','适合这样的照片'],['unsuitable','不太适合'],['tips','上传前这样选']])if(item.guide&&Array.isArray(item.guide[key])&&item.guide[key].length){content.append(node('h3','',title));const list=node('ul','creation-advice');item.guide[key].forEach(text=>list.append(node('li','',text)));content.append(list);}
    try{content.append(note('轻量 '+price(session.config,'photo','light')+' / 精细 '+price(session.config,'photo','fine')+' 光子'));}catch(e){content.append(note('费用在生成前以最新价格确认'));}
    add(content,button('使用这个风格',()=>{dialog.close();ctx.navigate('create',{templateId:item.id});}),button('复制模板链接',run(async()=>{const url=new URL(root.ownerDocument.baseURI);url.search='';url.hash='create?templateId='+encodeURIComponent(item.id);const clipboard=root.ownerDocument.defaultView.navigator.clipboard;if(clipboard){await clipboard.writeText(url.href);ctx.toast('模板链接已复制');}else{const value=node('input','creation-input');value.value=url.href;value.readOnly=true;content.append(value);value.focus();value.select();ctx.toast('已选中模板链接，可复制分享');}}),'creation-link-button'));dialog.append(content);root.append(dialog);dialog.addEventListener('close',()=>dialog.remove());dialog.showModal();headingTitle.focus({preventScroll:true});dialog.scrollTop=0;
  }
  let catalogVersion=0;
  async function loadCatalog(force=false){const version=++catalogVersion,requested=draft.template_id||'';templateError='';try{const found=draft.input_mode==='text'&&route!=='templates'&&!requested?null:await session.catalog(requested,force);if(version!==catalogVersion||requested!==draft.template_id)return;target=found;if(target&&!Object.keys(draft.text_fields||{}).length)(target.text_fields||[]).forEach(f=>draft.text_fields[f.key]=f.default||'');if(!session.config){session.config=await ctx.api('/api/config');session.assertActive();}if(account(ctx)&&route==='templates')await session.loadPreferences();}catch(e){if(!active()||version!==catalogVersion||requested!==draft.template_id)return;templateError=e.message||'风格加载失败，请稍后重试';target=null;}if(active())render();}
  async function restore(value){const restored=Object.assign(freshDraft(value.input_mode),value,{rights:false});if(restored.input_mode==='text'&&route!=='text'){ctx.navigate('text',{initialDraft:restored});return;}
    draft=restored;if(draft.template_id){try{target=await session.catalog(draft.template_id);templateError='';}catch(e){target=null;templateError=e.message;}}if(active())render();}
  async function chooseFile(file){if(!file||!/^image\//.test(file.type||''))throw problem('请选择照片文件');const previous=draft.blob;draft.blob=file;draft.rights=false;if(!await login()){if(ctx.accountVersion===session.version)draft.blob=previous;return;}const receipt=session.receipt();if(receipt&&receipt.state!=='accepted'&&!retryable)throw problem('上次提交仍待确认，暂不更换原图');
    if(receipt&&receipt.state==='accepted')await session.newCreation();draft.blob=file;draft.rights=false;await session.saveDraft(Object.assign({},draft,{rights:false}));render();}
  async function switchMode(mode){
    if(!active()||formBusy||mode===draft.input_mode)return;
    const savedModes=params.modeDraftsOwner===session.owner?Object.assign({},params.modeDrafts):{};
    savedModes[draft.input_mode]=Object.assign({},draft,{text_fields:Object.assign({},draft.text_fields),rights:false});
    if(session.owner&&(draft.blob||(draft.prompt||'').trim()))await session.saveDraft(savedModes[draft.input_mode]);
    if(!active())return;
    ctx.navigate(mode==='text'?'text':'create',{modeDraftsOwner:session.owner,modeDrafts:savedModes,initialDraft:savedModes[mode]||freshDraft(mode)});
  }
  function renderForm(){
    let pending;
    try{pending=session.receipt();}catch(e){status=e.message;pending={state:'unreadable'};}
    root.replaceChildren();add(root,node('p','creation-eyebrow',draft.input_mode==='text'?'文字生图':'照片创作'),node('h1','',draft.input_mode==='text'?'描述心中的画面':'让旧日瞬间，重现眼前'));
    const modes=node('nav','creation-tabs');modes.setAttribute('aria-label','选择创作方式');
    [['photo','照片创作'],['text','文字生图']].forEach(([mode,label])=>{const entry=button(label,run(()=>switchMode(mode)),'creation-tab'+(mode===draft.input_mode?' is-active':''));entry.dataset.action='mode-'+mode;entry.setAttribute('aria-current',mode===draft.input_mode?'page':'false');entry.disabled=formBusy;modes.append(entry);});root.append(modes);
    const grid=node('div','creation-form-grid'),preview=node('section','creation-preview-column'),form=node('section','creation-form-column');
    if(status)add(form,node('div','creation-state',status));
    if(templateError)add(form,node('div','creation-state',templateError),button('重试所选风格',run(()=>loadCatalog(true))));
    if(draft.input_mode==='photo'){
      const picker=node('input','creation-file');picker.type='file';picker.accept='image/*';picker.setAttribute('aria-label','选择照片');picker.disabled=formBusy;picker.addEventListener('change',run(async()=>{await chooseFile(picker.files&&picker.files[0]);}));
      add(preview,field('选择照片',picker,'选图与个人草稿需要登录；照片不会自动公开到社区。'));
      if(draft.blob){const stage=node('div','creation-photo-stage');const img=node('img','creation-photo-preview');const url=URL.createObjectURL(draft.blob);urls.add(url);img.src=url;img.alt='当前照片取景预览';stage.style.aspectRatio=draft.aspect_ratio==='original'||draft.template_id?'auto':draft.aspect_ratio.replace(':',' / ');img.addEventListener('load',()=>{if(active()&&(draft.aspect_ratio==='original'||draft.template_id))stage.style.aspectRatio=img.naturalWidth+' / '+img.naturalHeight;});stage.append(img);preview.append(stage);}
      else preview.append(node('div','creation-photo-empty','选择一张照片，开始这次创作'));
      preview.append(note(draft.template_id?'素材保持完整，成品按模板构图。':'非原图比例会居中裁切，保持原图则保留原始比例。'));
      const styles=node('select','creation-input');const original=node('option','','原片修复');original.value='';original.selected=!draft.template_id;styles.append(original);session.templates.forEach(item=>{const option=node('option','',item.name);option.value=item.id;option.selected=item.id===draft.template_id;styles.append(option);});
      if(draft.template_id&&!target){const unavailable=node('option','','所选风格暂不可用');unavailable.value=draft.template_id;unavailable.selected=true;styles.append(unavailable);}
      styles.disabled=formBusy||!!(pending&&pending.state!=='accepted');styles.addEventListener('change',()=>{draft.template_id=styles.value;target=session.templates.find(t=>t.id===styles.value)||null;templateError='';draft.template_output_mode='template';draft.text_fields={};(target&&target.text_fields||[]).forEach(f=>draft.text_fields[f.key]=f.default==='{today}'?new Date().toLocaleDateString('sv-SE').replace(/-/g,'.'):f.default||'');if(target)draft.aspect_ratio='original';scheduleDraft();render();});
      add(form,field('创作方式',styles));if(target)form.append(button('查看风格示例与选图建议',()=>detail(target),'creation-link-button'));
      if(!draft.template_id)form.append(field('画幅比例',select(PHOTO_RATIOS,draft.aspect_ratio,v=>{draft.aspect_ratio=v;render();}),'非原图比例按画面中心裁切。'));
      const quality=select(['light','fine'],draft.quality,v=>{draft.quality=v;render();});
      let light='价格加载中',fine='价格加载中';try{light=price(session.config,'photo','light')+' 光子';fine=price(session.config,'photo','fine')+' 光子';}catch(e){}
      quality.options[0].textContent='轻量生成 · '+light+' · 速度优先';quality.options[1].textContent='精细生成 · '+fine+' · 细节优先';form.append(field('生成档位',quality));
      if(target){const output=select(['template','single'],draft.template_output_mode,v=>{draft.template_output_mode=v;render();});output.options[0].textContent='按模板生成';output.options[1].textContent='仅生成后图';output.options[1].disabled=!(session.config&&session.config.template_output_modes||[]).includes('single');add(form,field('成品形式',output,'仅后图会要求省略原图对比区域，不是从已有拼图精确裁切。费用不变。'));
        (target.text_fields||[]).forEach(f=>form.append(field(f.label||f.key,input(draft.text_fields[f.key]||'',v=>draft.text_fields[f.key]=v,false,f.max_len||100))));
      }else{const toggle=node('input','');toggle.type='checkbox';toggle.checked=draft.show_custom_prompt;toggle.addEventListener('change',()=>{draft.show_custom_prompt=toggle.checked;scheduleDraft();render();});form.append(field('补充要求 · 可选',toggle,'留空会自动优化清晰度、曝光与色彩；收起后不提交补充文字。'));if(draft.show_custom_prompt)form.append(field('明确描述修改要求',input(draft.custom_prompt,v=>draft.custom_prompt=v,true)));}
      const rights=node('input','');rights.type='checkbox';rights.checked=!!draft.rights;rights.dataset.confirmRights='true';rights.addEventListener('change',()=>draft.rights=rights.checked);form.append(field('我拥有这张照片的使用权',rights,'恢复草稿或更换照片后需重新确认。'));
    }else{
      preview.append(note('写清主体、场景和风格；不需要上传照片。'));const prompt=input(draft.prompt,v=>draft.prompt=v,true);prompt.placeholder='例如：雨后的森林小屋，暖色灯光，水彩插画风格。';form.append(field('画面描述（最多 500 字）',prompt));
      const examples=node('div','creation-tabs');EXAMPLES.forEach(([title,text])=>examples.append(button(title,run(async()=>{if(draft.prompt.trim()&&!await ctx.confirm('示例会替换当前描述，是否继续？'))return;draft.prompt=text;scheduleDraft();render();}),'creation-tab')));add(form,examples,field('画面比例',select(TEXT_RATIOS,draft.aspect_ratio,v=>draft.aspect_ratio=v)));
      if(!session.config||!session.config.text_generation||!session.config.text_generation.ready)form.append(note('文字生图暂未开放，当前不会提交或扣费。'));
    }
    const cost=money();const footer=node('div','creation-submit-bar');const balanceText=ctx.state.user?'余额 '+(Number.isFinite(ctx.state.user.balance)?ctx.state.user.balance:'—')+' 光子':'注册后创作 · 初始 0 光子仅由后台增加';add(footer,note(balanceText+' · '+(cost===null?'价格加载中':'本次预扣 '+cost+' 光子')));
    const submit=button(formBusy?'正在提交…':retryable?'沿用原编号重新提交':'开始生成',run(async()=>{
      if(!await login())return;formBusy=true;status='正在保存草稿与提交回执…';render();
      try{const created=await session.submit(draft,cost,{retry:retryable});if(!active()||!created)return;knownJob=created.job_id;status='任务已受理，可以离开到作品页查看';render();await wait(knownJob);}catch(e){if(e.code==='UNCERTAIN'){try{const found=await session.lookup();retryable=!!(found&&found.state==='not_found');}catch(ignore){}}error(e);}finally{formBusy=false;if(active())render();}
    }));submit.disabled=formBusy||cost===null||!!(draft.template_id&&!target)||!!(pending&&pending.state==='unreadable')||!!(draft.input_mode==='text'&&!(session.config&&session.config.text_generation&&session.config.text_generation.ready));footer.append(submit);
    if(pending&&pending.state!=='accepted'){
      footer.append(button('核对上次提交',run(async()=>{const found=await session.lookup();if(found&&found.state==='accepted'){knownJob=found.job_id;await wait(knownJob);}else {retryable=!!(found&&found.state==='not_found');status=retryable?'尚未查到登记，可手动沿用原编号提交；参数保持不变':found&&found.state==='rejected'?'上次提交明确未受理，可重新创作':'仍待确认，请稍后再次核对；不会自动重新付费';render();}}),'creation-link-button'));
    }
    form.querySelectorAll('input,textarea,select').forEach(el=>{if(formBusy||(pending&&pending.state!=='accepted'&&!(retryable&&(el.type==='file'||el.dataset.confirmRights))))el.disabled=true;});
    if(knownJob||pending&&pending.job_id){footer.append(button('到作品页查看进度',()=>ctx.navigate('works',{jobId:knownJob||pending.job_id}),'creation-link-button'));footer.append(button('开始新的创作',run(async()=>{await session.newCreation();knownJob='';status='';retryable=false;draft=freshDraft(draft.input_mode);target=null;render();}),'creation-link-button'));}
    add(form,footer);add(grid,preview,form);root.append(grid);
  }
  async function wait(id){const job=await session.poll(id,j=>{if(active()){status=STAGES[j.stage]||'任务处理中，可以稍后到作品页查看';render();}});if(!active())return;if(job&&job.status==='succeeded')ctx.navigate('result',{jobId:id});else if(job&&job.status==='failed'){ctx.invalidateAccount();status=(job.error||'生成未完成')+'；请在额度流水中核对返还';render();}else{status='任务仍在云端处理，可到作品页查看，不需要再次提交';render();}}
  async function download(url,jobId){
    let link=await session.resultMedia(jobId,'result',url);for(let i=0;i<2;i++){const response=await session.fetch(directMedia(link),{credentials:'omit'});session.assertActive();if(response.ok){const blob=await imageBlob(await response.blob());session.assertActive();const local=URL.createObjectURL(blob);urls.add(local);const anchor=node('a','');anchor.href=local;anchor.download=jobId+(blob.type==='image/png'?'.png':blob.type==='image/webp'?'.webp':'.jpg');anchor.click();return;}if(i===0&&[401,403].includes(response.status)){const fresh=await ctx.api('/api/jobs/'+encodeURIComponent(jobId));session.assertActive();if(!fresh.result_url)throw problem('作品已到保存期限');link=await session.resultMedia(jobId,'result',fresh.result_url);}else throw problem('图片下载未完成，请稍后重试',response.status);}
  }
  async function renderResult(){
    root.replaceChildren(node('p','creation-state','正在读取作品…'));if(!await login())return;const id=params.jobId;if(!id)throw problem('缺少作品编号，请重新打开作品');const job=await ctx.api('/api/jobs/'+encodeURIComponent(id));session.assertActive();
    if(job.status!=='succeeded'){knownJob=id;status=job.status==='failed'?job.error||'生成未完成':'作品仍在生成，可以到作品页查看进度';root.replaceChildren(note(status),button('查看作品',()=>ctx.navigate('works',{jobId:id})));return;}
    if(!job.result_url)throw problem('作品已到保存期限');const resultUrl=await session.resultMedia(id,'result',job.result_url);root.replaceChildren(node('p','creation-eyebrow','作品典藏'),node('h1','','这次创作的结果'));
    const stage=node('div','creation-result-stage');if(job.width&&job.height)stage.style.aspectRatio=job.width+' / '+job.height;const result=node('img','creation-result-image');result.src=resultUrl;result.alt='生成后的成品';stage.append(result);root.append(stage);
    if(job.orig_url&&job.input_mode!=='text'){
      try{const originalUrl=await session.resultMedia(id,'orig',job.orig_url),before=node('img','creation-result-image creation-before-image');before.src=originalUrl;before.alt=job.comparison_compressed?'对比原图（压缩版）':'原图';before.style.clipPath='inset(0 50% 0 0)';stage.append(before);const slider=node('input','creation-comparison-slider');slider.type='range';slider.min='0';slider.max='100';slider.value='50';slider.setAttribute('aria-label','拖动查看原图和生成结果');slider.addEventListener('input',()=>before.style.clipPath='inset(0 '+(100-Number(slider.value))+'% 0 0)');root.append(note('左右拖动对比'+(job.comparison_compressed?' · 对比原图为压缩版':'')),slider);}
      catch(e){if(e.code==='INACTIVE')throw e;root.append(note('成品已显示；原图暂未提供 COS 直链：'+e.message));}
    }else root.append(note(job.input_mode==='text'?'文字生图成品':'原图已到期，当前仅展示成品'));
    const dock=node('div','creation-result-actions');add(dock,button('高清预览',()=>{const dialog=node('dialog','creation-dialog creation-zoom-dialog'),image=node('img','creation-zoom-image');image.src=result.src;image.alt='高清成品预览';const scale=node('input','creation-input');scale.type='range';scale.min='100';scale.max='400';scale.value='100';scale.setAttribute('aria-label','调整高清预览放大比例');scale.addEventListener('input',()=>image.style.width=scale.value+'%');add(dialog,button('关闭预览',()=>dialog.close(),'creation-link-button'),scale,image);root.append(dialog);dialog.addEventListener('close',()=>dialog.remove());dialog.showModal();}),button('下载图片',run(async()=>{await download(job.result_url,id);ctx.toast('图片已开始下载');})),button('沿用参数再创作',run(async()=>{const recipe=await ctx.api('/api/jobs/'+encodeURIComponent(id)+'/recipe');session.assertActive();if(!complete(recipe))throw problem('旧作品参数不完整，请重新选图或填写描述');await session.newCreation();ctx.navigate(recipe.input_mode==='text'?'text':'create',{recipe});})));root.append(dock);
    let imageRetry=0;result.addEventListener('error',run(async()=>{if(imageRetry++){ctx.toast('成品暂未加载，请返回作品页重试');return;}const fresh=await ctx.api('/api/jobs/'+encodeURIComponent(id));session.assertActive();if(!fresh.result_url)throw problem('作品已到保存期限');result.src=await session.resultMedia(id,'result',fresh.result_url);}));
  }
  function render(){if(!active())return;for(const url of urls)URL.revokeObjectURL(url);urls.clear();if(route==='templates')renderTemplates();else if(route!=='result')renderForm();}
  root.classList.add('creation-module');
  if(['create','text'].includes(route))root.classList.add('creation-form-page');
  const initialize=async()=>{try{
    if(route==='result'){await renderResult();}
    else {
      if(params.initialDraft)draft=Object.assign(draft,params.initialDraft,{rights:false});
      if(params.templateId)draft.template_id=params.templateId;
      if(params.recipe||params.jobId){if(await login())draft=await session.recipe(params.recipe||params.jobId);}
      if(route==='text'&&draft.input_mode!=='text')draft=freshDraft('text');
      if(route==='create'&&draft.input_mode==='text'){ctx.navigate('text',{initialDraft:draft});return ()=>session.close();}
      await loadCatalog();if(!active())return ()=>session.close();
      if(session.owner){
        try{const saved=await session.readDraft();if(saved&&params.restoreDraft)await restore(saved);else if(saved&&!(params.initialDraft||params.recipe||params.jobId)){const banner=node('div','creation-draft-banner');add(banner,note('你有一份未完成的'+(saved.input_mode==='text'?'文字':'照片')+'草稿'),button('继续上次创作',run(async()=>{if(saved.input_mode==='text'&&route!=='text'){ctx.navigate('text',{initialDraft:saved});return;}if(saved.input_mode==='photo'&&route!=='create'){ctx.navigate('create',{initialDraft:saved});return;}await restore(saved);}),'creation-link-button'));root.prepend(banner);}}
        catch(e){status=e.message;ctx.toast(status);}
        const receipt=session.receipt();if(receipt&&receipt.job_id){knownJob=receipt.job_id;status='上次任务已受理，可查看进度或明确开始新创作';render();}
        else if(receipt){draft=Object.assign(draft,receipt.draft,{blob:draft.blob,rights:false});const saved=await session.readDraft();if(saved&&saved.input_mode===receipt.input_mode)draft.blob=saved.blob;status='上次提交尚待确认，核对前不会再次扣费';render();}
      }
    }
  }catch(e){error(e);}};
  root.replaceChildren(note('正在准备创作空间…'));initialize().catch(error);
  return ()=>{if(active()&&dirty&&session.owner&&['create','text'].includes(route))session.saveDraft(Object.assign({},draft,{rights:false})).catch(()=>{});disposed=true;session.close();clearTimeout(saveTimer);for(const url of urls)URL.revokeObjectURL(url);urls.clear();root.querySelectorAll('dialog').forEach(dialog=>dialog.close());};
}
