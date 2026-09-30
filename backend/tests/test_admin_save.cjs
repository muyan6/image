/* Admin form execution with an isolated DOM/server; no real credentials or network. */
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const root=path.resolve(process.env.REVIEW_ROOT||path.resolve(__dirname,'../..'));
const output=path.resolve(process.env.REVIEW_OUTPUT||path.join(root,'audit/admin-save-tests'));
fs.mkdirSync(output,{recursive:true});
const html=fs.readFileSync(path.join(root,'backend/admin.html'),'utf8');
const script=[...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)].map(x=>x[1]).join('\n');
const defaults={providers:{worldcodes:{enabled:true,base_url:'https://legacy.invalid',api_key:'••••0000',endpoint:'/v1/images/edits',model_light:'light',model_fine:'fine',timeout:180,tiers:{
 light:{base_url:'https://light.invalid',api_key:'••••1234',endpoint:'/light/edits',model:'light',timeout:60,price_cny:.04},
 fine:{base_url:'https://fine.invalid',api_key:'••••5678',endpoint:'/fine/edits',model:'fine',timeout:300,price_cny:.15}}},
 fal:{enabled:false,api_key:''},baidu:{enabled:false,api_key:'',secret_key:''},local:{enabled:true}},
 chain:['worldcodes','local','fal','baidu'],prompts:{light:'light prompt',fine:'fine prompt'},prices:{light:40,fine:100},
 text_generation:{enabled:true,model:'text-model',endpoint:'/v1/images/generations',price:40},processing:{ci_enabled:true},
 cloud_pipeline:{enabled:true,generation_concurrency:32,max_queued:256,ci_biz_type:''},rewards:{},maintenance:{enabled:false},free_mode:false,ads:{},moderation:{enabled:true},wechat:{},payment:{},tencent:{},quota:{}};
function setup(){
 const nodes=new Map(),calls=[],listeners={};let slot=null,deny=false,pending=null,confirmResult=true;
 class E{constructor(id){this.id=id;this.value='';this.checked=false;this.disabled=false;this.hidden=false;this.dataset={};this.children=[];this.classList={add(){},remove(){},toggle(){}};this.textContent='';}
  set value(v){this._value=String(v??'');}get value(){return this._value;}
  appendChild(n){if(n.parentNode)n.parentNode.children=n.parentNode.children.filter(x=>x!==n);this.children.push(n);n.parentNode=this;return n;}
  set innerHTML(text){this.html=text;if(this.id==='modal-root'){
   const m=text.match(/data-save-slot data-save-function="([^"]+)"(?: data-save-id="([^"]*)")?/);slot=m?new E('slot'):null;
   if(slot)slot.dataset={saveFunction:m[1],saveId:m[2]||''};}}
  get innerHTML(){return this.html||'';}
  querySelectorAll(){return [...nodes.values()].filter(n=>n.id.startsWith('wc-')||n.id.startsWith('tg-'));}
  closest(){return this.section||null;}
  addEventListener(){}
 }
 const get=id=>{if(!nodes.has(id))nodes.set(id,new E(id));return nodes.get(id);};
 const document={getElementById:get,createElement:()=>new E('generated'),querySelector:()=>slot,
  querySelectorAll:()=>[],addEventListener:(name,fn)=>{listeners[name]=fn;}};
 const ctx={document,console,location:{origin:'https://fixture.invalid'},setTimeout:()=>0,clearTimeout(){},setInterval:()=>0,
  confirm:()=>confirmResult,fetch:async(url,opts)=>{
   if(url.endsWith('/session'))return {ok:false,status:401,json:async()=>({})};
   if(url.endsWith('/announcements'))return {ok:true,status:200,json:async()=>({items:[]})};
   calls.push({url,...opts,body:opts.body?JSON.parse(opts.body):null});
   if(pending)return new Promise(resolve=>pending.resolve=resolve);
   if(deny)return {ok:false,status:400,json:async()=>({detail:'fixture validation error'})};
   return {ok:true,status:200,json:async()=>JSON.parse(JSON.stringify(defaults))};
  }};ctx.window=ctx;
 vm.createContext(ctx);vm.runInContext(script,ctx);
 vm.runInContext('SETTINGS='+JSON.stringify(defaults),ctx);
 return {ctx,get,calls,listeners,setDeny:x=>deny=x,setConfirm:x=>confirmResult=x,
  defer:()=>pending={},release:()=>{pending.resolve({ok:true,status:200,json:async()=>defaults});pending=null;}};
}
const rows=[];async function test(name,fn){try{await fn();rows.push({case:name,passed:true});}catch(e){console.error(name,e);rows.push({case:name,passed:false});}}
(async()=>{
 await test('admin_has_one_physical_save_button_and_no_card_save_buttons',()=>{
  assert.equal((html.match(/id="page-save"/g)||[]).length,1);
  for(const fn of ['saveProviders','saveTextGeneration','saveConf','saveSec','saveTpl','saveCommunityPost','saveAnnouncement','saveGroup'])assert(!html.includes('onclick="'+fn+'('),fn);
  assert.equal((html.match(/data-save-slot data-save-function=/g)||[]).length,4);
 });
 await test('provider_page_one_put_saves_tiers_prompts_chain_text_together',async()=>{
  const t=setup();t.ctx.showPage('prov');t.get('wc-light-base').value='https://changed-light.invalid';t.get('wc-fine-key').value='new-fine-key';
  t.ctx.toggleProv('fal',{checked:true});t.ctx.moveProv(0,1);
  assert.equal(t.calls.length,0);await t.ctx.saveCurrentPage();assert.equal(t.calls.length,1);
  const body=t.calls[0].body;assert.equal(body.providers.worldcodes.tiers.light.base_url,'https://changed-light.invalid');
  assert.equal(body.providers.worldcodes.tiers.fine.api_key,'new-fine-key');assert(body.providers.fal.enabled);
  assert.equal(body.chain[0],'local');assert.equal(body.text_generation.model,'text-model');assert.equal(body.prompts.light,'light prompt');
 });
 await test('provider_toggle_and_reorder_do_not_discard_unsaved_inputs_or_autosave',()=>{
  const t=setup();t.ctx.showPage('prov');t.get('wc-light-key').value='typed-light-key';
  t.ctx.toggleProv('worldcodes',{checked:false});t.ctx.moveProv(0,1);
  assert.equal(t.get('wc-light-key').value,'typed-light-key');assert.equal(t.calls.length,0);
  assert.equal(vm.runInContext('SETTINGS.providers.worldcodes.enabled',t.ctx),true);
 });
 await test('save_failure_keeps_form_and_dirty_state',async()=>{
  const t=setup();t.ctx.showPage('prov');t.get('wc-fine-base').value='https://typed.invalid';t.ctx.markPageDirty();t.setDeny(true);
  await t.ctx.saveCurrentPage();assert.equal(t.get('wc-fine-base').value,'https://typed.invalid');
  assert(vm.runInContext('PAGE_DIRTY',t.ctx));assert(!t.get('page-save').disabled);
 });
 await test('double_click_save_sends_only_one_request',async()=>{
  const t=setup();t.ctx.showPage('prov');t.defer();const first=t.ctx.saveCurrentPage();await t.ctx.saveCurrentPage();
  assert.equal(t.calls.length,1);assert(t.get('page-save').disabled);t.release();await first;assert(!t.get('page-save').disabled);
 });
 await test('unsaved_navigation_can_be_cancelled',()=>{
  const t=setup();t.ctx.showPage('prov');t.ctx.markPageDirty();t.setConfirm(false);t.ctx.showPage('conf');
  assert.equal(vm.runInContext('CURRENT_PAGE',t.ctx),'prov');assert(vm.runInContext('PAGE_DIRTY',t.ctx));
 });
 await test('content_editor_moves_same_save_button_and_returns_it_on_close',async()=>{
  const t=setup();t.ctx.showPage('ann');let saved=false;t.ctx.saveAnnouncement=async()=>{saved=true;};
  t.ctx.openAnnModal(null);const bar=t.get('page-save-bar');assert.equal(bar.parentNode.id,'slot');
  await t.ctx.saveCurrentPage();assert(saved);t.ctx.closeModal();assert.equal(bar.parentNode.id,'page-save-home');assert(bar.hidden);
 });
 await test('cloud_mode_disables_only_irrelevant_traditional_processing_setting',()=>{
  const t=setup();t.ctx.showPage('conf');assert(t.get('ci-enabled').disabled);assert(!t.get('cloud-biz-type').disabled);
  t.get('cloud-enabled').checked=false;t.ctx.updateProcessingHint();assert(!t.get('ci-enabled').disabled);assert(t.get('cloud-biz-type').disabled);
  assert(t.get('ci-enabled').checked);
  assert(t.get('processing-mode-summary').textContent.includes('已保存：云端异步模式'));
  assert(t.get('processing-mode-summary').textContent.includes('当前草稿已切换为传统模式'));
 });
 await test('general_page_save_includes_rewards_ads_and_cloud_settings_once',async()=>{
  const t=setup();t.ctx.showPage('conf');await t.ctx.saveCurrentPage();assert.equal(t.calls.length,1);
  const b=t.calls[0].body;for(const k of ['prices','rewards','processing','cloud_pipeline','maintenance','free_mode','ads'])assert(k in b,k);
  assert.equal(b.cloud_pipeline.generation_concurrency,32);
  assert.equal(b.cloud_pipeline.audit_mode,'wechat_auto');
 });
 await test('security_page_save_includes_all_platform_credentials_once',async()=>{
  const t=setup();t.ctx.showPage('sec');await t.ctx.saveCurrentPage();assert.equal(t.calls.length,1);
  for(const k of ['wechat','payment','tencent','moderation','quota'])assert(k in t.calls[0].body,k);
 });
 await test('audit_help_explains_strategy_default_and_actual_picture_engine',()=>{
  assert(html.includes('留空使用腾讯云默认审核策略'));
  assert(html.includes('微信 COS 链接审核优先'));assert(html.includes('仅控制基础图片处理，不控制审核'));
  assert(html.includes('audit-tencent/input/'));assert(html.includes('/api/callbacks/cos-audit'));
 });
 fs.writeFileSync(path.join(output,'admin_save_results.json'),JSON.stringify({cases:rows},null,2));
 const failed=rows.filter(x=>!x.passed).length;console.log(`ADMIN_SAVE_SUMMARY total=${rows.length} passed=${rows.length-failed} failed=${failed}`);process.exitCode=failed?1:0;
})();
