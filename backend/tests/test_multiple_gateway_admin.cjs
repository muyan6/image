/* Real admin DOM and script; anonymous fixtures, no network or real credentials. */
const fs=require('fs'),path=require('path'),assert=require('assert'),vm=require('vm');
const {JSDOM}=require(process.env.WEB_DOM_MODULE||'jsdom');
const root=path.resolve(process.env.REVIEW_ROOT||path.resolve(__dirname,'../..'));
const out=path.resolve(process.env.REVIEW_OUTPUT||path.join(root,'audit/multiple-gateway-admin-tests'));
const html=fs.readFileSync(path.join(root,'backend/admin.html'),'utf8');
const script=[...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)].map(x=>x[1]).join('\n');
const secondary='gw_'+'a'.repeat(32),third='gw_'+'b'.repeat(32),rows=[];
const tier=(quality,mask)=>({enabled:true,base_url:'https://'+quality+'.invalid',api_key:'••••'+mask,endpoint:'/v1/images/'+quality,model:'model-'+quality,timeout:60,price_cny:.12,request_mode:'async',async_endpoint:''});
function fixture(){return {chain:['worldcodes',secondary],providers:{worldcodes:{name:'主网关',enabled:true,tiers:{light:tier('light','1111'),fine:tier('fine','2222')}},[secondary]:{name:'备用网关',enabled:false,tiers:{light:tier('backup-light','3333'),fine:tier('backup-fine','4444')}}},prompts:{light:'fixture light prompt',fine:'fixture fine prompt'},text_generation:{enabled:true,base_url:'https://text.invalid',api_key:'••••5555',endpoint:'/v1/images/generations',model:'text-model',timeout:90,price_cny:.2,price:40},prices:{light:40,fine:40},moderation:{enabled:true},processing:{ci_enabled:true},cloud_pipeline:{enabled:true,generation_concurrency:16,max_queued:64},maintenance:{enabled:false},rewards:{},free_mode:false,ads:{},wechat:{},payment:{},tencent:{},quota:{}};}
function setup(data=fixture()){
 const dom=new JSDOM(html,{url:'https://fixture.invalid/admin',runScripts:'outside-only'}),w=dom.window,calls=[];let reject=false,confirmation=true;
 let stored=structuredClone(data),overview={stats:{today_total:4,today_succeeded:3,today_failed:1,running:0,today_cost_cny:1.25,total_cost_cny:23.5,total_cost_usd:.7},chain:stored.chain,providers:Object.fromEntries(stored.chain.map(id=>[id,{name:stored.providers[id].name,enabled:stored.providers[id].enabled,configured:true,light_ready:true,fine_ready:true}])),configured:{},maintenance:{enabled:false},cos_ready:true,moderation_detail:{enabled:false}};
 let jobs=[];
 w.confirm=()=>confirmation;w.alert=()=>{};w.setTimeout=()=>0;w.clearTimeout=()=>{};w.setInterval=()=>0;
 w.fetch=async(url,opts={})=>{
  if(url.endsWith('/session'))return {ok:false,status:401,json:async()=>({})};
  if(url.endsWith('/overview'))return {ok:true,status:200,json:async()=>overview};
  if(url.includes('/jobs?'))return {ok:true,status:200,json:async()=>({items:jobs,total:jobs.length})};
  if(url.endsWith('/settings')&&opts.method==='PUT'){
   const body=JSON.parse(opts.body);calls.push(body);
   if(reject)return {ok:false,status:400,json:async()=>({detail:'fixture validation failure'})};
   for(const [id,conf] of Object.entries(body.providers||{})){if(conf===null)delete stored.providers[id];else stored.providers[id]=structuredClone(conf);}
   if(body.chain)stored.chain=[...body.chain];if(body.prompts)stored.prompts=body.prompts;if(body.text_generation)stored.text_generation=body.text_generation;
   return {ok:true,status:200,json:async()=>structuredClone(stored)};
  }
  return {ok:true,status:200,json:async()=>({items:[]})};
 };
 const context=dom.getInternalVMContext();vm.runInContext(script,context);vm.runInContext('SETTINGS='+JSON.stringify(stored),context);w.showPage('prov');
 return {w,ctx:context,dom,calls,get:id=>w.document.getElementById(id),setReject:x=>reject=x,setConfirm:x=>confirmation=x,setOverview:x=>overview=x,setJobs:x=>jobs=x,close:()=>dom.window.close()};
}
async function test(name,fn){let f;try{f=setup();await fn(f);rows.push({case:name,passed:true});console.log('PASS '+name);}catch(error){rows.push({case:name,passed:false,error:error.stack});console.error('FAIL '+name,error);}finally{f?.close();}}
module.exports={setup,fixture,secondary,third,html,script};
if(require.main===module)(async()=>{
 await test('legacy_modes_are_not_configuration_entries',f=>{
  for(const id of ['fal-key','bd-key','bd-sk'])assert.equal(f.get(id),null);
  assert(!f.get('page-prov').textContent.includes('传统模式'));assert(!f.get('page-prov').textContent.includes('fal.ai'));
  assert.equal(f.get('chain-list').querySelectorAll('.gateway-row').length,2);
 });
 await test('add_gateway_uses_uuid_id_sync_defaults_and_no_immediate_save',f=>{
  f.w.addGateway();const draft=f.w.eval('PROVIDER_DRAFT');assert.equal(draft.chain.length,3);
  assert(/^gw_[0-9a-f]{32}$/.test(draft.active));assert.equal(f.get('wc-light-mode').value,'sync');assert.equal(f.get('wc-fine-mode').value,'sync');
  assert.equal(draft.providers[draft.active].enabled,false);assert.equal(f.calls.length,0);
 });
 await test('rename_enabled_and_move_preserve_current_form_draft',f=>{
  f.get('gateway-name').value='自定义首选';f.get('gateway-name').dispatchEvent(new f.w.Event('input',{bubbles:true}));
  f.get('wc-light-key').value='fixture-new-light';f.w.toggleProv(secondary,{checked:true});f.w.moveProv(1,-1);
  assert.equal(f.w.eval('PROVIDER_DRAFT.chain[0]'),secondary);assert.equal(f.get('wc-light-key').value,'fixture-new-light');assert(f.get('chain-list').textContent.includes('自定义首选'));
  assert.equal(f.calls.length,0);assert.equal(f.w.eval('SETTINGS.providers.worldcodes.name'),'主网关');
 });
 await test('switching_gateways_preserves_both_tiers_and_transport_paths',f=>{
  f.get('wc-light-base').value='https://typed-primary.invalid';f.get('wc-light-key').value='fixture-primary-key';f.get('wc-fine-key').value='fixture-fine-key';
  f.get('wc-light-mode').value='sync';f.get('wc-fine-async').value='/custom/fine/task';f.w.selectGateway(secondary);
  f.get('wc-light-key').value='fixture-backup-key';f.w.selectGateway('worldcodes');
  assert.equal(f.get('wc-light-base').value,'https://typed-primary.invalid');assert.equal(f.get('wc-light-key').value,'fixture-primary-key');assert.equal(f.get('wc-fine-key').value,'fixture-fine-key');
  assert.equal(f.get('wc-light-mode').value,'sync');assert.equal(f.get('wc-fine-async').value,'/custom/fine/task');f.w.selectGateway(secondary);assert.equal(f.get('wc-light-key').value,'fixture-backup-key');
 });
 await test('one_put_saves_all_gateways_chain_and_independent_text_configuration',async f=>{
  f.get('wc-light-endpoint').value='/custom/edit.php';f.get('wc-fine-endpoint').value='/separate/fine';f.w.selectGateway(secondary);f.get('wc-light-base').value='https://backup.changed.invalid';
  f.get('tg-base').value='https://text.changed.invalid';await f.w.saveCurrentPage();assert.equal(f.calls.length,1);const body=f.calls[0];
  assert.equal(body.providers.worldcodes.tiers.light.endpoint,'/custom/edit.php');assert.equal(body.providers.worldcodes.tiers.fine.endpoint,'/separate/fine');
  assert.equal(body.providers[secondary].tiers.light.base_url,'https://backup.changed.invalid');assert.equal(body.text_generation.base_url,'https://text.changed.invalid');assert.equal(body.text_generation.api_key,'••••5555');
  assert.equal(body.prompts.light,'fixture light prompt');assert.equal(body.chain.join(','),'worldcodes,'+secondary);
 });
 await test('two_gateways_four_masked_keys_roundtrip_without_becoming_empty',async f=>{
  await f.w.saveCurrentPage();const providers=f.calls[0].providers;
  assert.equal(providers.worldcodes.tiers.light.api_key,'••••1111');assert.equal(providers.worldcodes.tiers.fine.api_key,'••••2222');
  assert.equal(providers[secondary].tiers.light.api_key,'••••3333');assert.equal(providers[secondary].tiers.fine.api_key,'••••4444');
 });
 await test('clear_key_is_explicit_and_only_affects_selected_gateway_and_tier',async f=>{
  f.w.selectGateway(secondary);f.w.clearGatewayKey('fine');await f.w.saveCurrentPage();const providers=f.calls[0].providers;
  assert.equal(providers[secondary].tiers.fine.api_key,'');assert.equal(providers[secondary].tiers.light.api_key,'••••3333');assert.equal(providers.worldcodes.tiers.fine.api_key,'••••2222');
 });
 await test('tier_enable_flags_remain_independent_across_gateway_switches',async f=>{
  f.get('wc-light-enabled').checked=false;f.w.selectGateway(secondary);f.get('wc-fine-enabled').checked=false;f.w.selectGateway('worldcodes');
  assert.equal(f.get('wc-light-enabled').checked,false);assert.equal(f.get('wc-fine-enabled').checked,true);await f.w.saveCurrentPage();
  const p=f.calls[0].providers;assert.equal(p.worldcodes.tiers.light.enabled,false);assert.equal(p[secondary].tiers.fine.enabled,false);assert.equal(p[secondary].tiers.light.enabled,true);
 });
 await test('delete_worldcodes_with_another_gateway_is_single_atomic_patch',async f=>{
  f.w.deleteGateway('worldcodes');assert.equal(f.calls.length,0);assert.equal(f.w.eval('PROVIDER_DRAFT.active'),secondary);
  await f.w.saveCurrentPage();assert.equal(f.calls.length,1);assert.equal(f.calls[0].providers.worldcodes,null);assert.equal(f.calls[0].chain.join(','),secondary);
 });
 await test('last_gateway_cannot_be_deleted_and_all_can_be_disabled',async f=>{
  f.w.deleteGateway(secondary);f.w.deleteGateway('worldcodes');assert.equal(f.w.eval('PROVIDER_DRAFT.chain.length'),1);
  f.w.toggleProv('worldcodes',{checked:false});await f.w.saveCurrentPage();assert.equal(f.calls[0].providers.worldcodes.enabled,false);
 });
 await test('delete_new_unsaved_gateway_does_not_send_unnecessary_delete',async f=>{
  f.w.addGateway();const id=f.w.eval('PROVIDER_DRAFT.active');f.w.deleteGateway(id);await f.w.saveCurrentPage();assert(!(id in f.calls[0].providers));
 });
 await test('save_failure_keeps_all_gateway_drafts_selected_id_and_dirty_state',async f=>{
  f.get('wc-light-key').value='fixture-primary-edit';f.w.selectGateway(secondary);f.get('wc-fine-base').value='https://typed.backup.invalid';f.w.markPageDirty();f.setReject(true);
  await f.w.saveCurrentPage();assert.equal(f.w.eval('PROVIDER_DRAFT.active'),secondary);assert.equal(f.get('wc-fine-base').value,'https://typed.backup.invalid');assert(f.w.eval('PAGE_DIRTY'));
  f.w.selectGateway('worldcodes');assert.equal(f.get('wc-light-key').value,'fixture-primary-edit');assert.equal(f.get('page-save').disabled,false);
 });
 await test('save_success_retains_selected_gateway_and_clears_only_dirty_flag',async f=>{
  f.w.selectGateway(secondary);f.get('gateway-name').value='新备用名';f.w.markPageDirty();await f.w.saveCurrentPage();
  assert.equal(f.w.eval('PROVIDER_DRAFT.active'),secondary);assert.equal(f.get('gateway-name').value,'新备用名');assert.equal(f.w.eval('PAGE_DIRTY'),false);
 });
 await test('same_page_navigation_does_not_discard_dirty_provider_inputs',f=>{
  f.get('wc-light-key').value='fixture-dirty-key';f.w.markPageDirty();f.w.showPage('prov');assert.equal(f.get('wc-light-key').value,'fixture-dirty-key');assert(f.w.eval('PAGE_DIRTY'));
 });
 await test('existing_dirty_leave_confirmation_is_preserved',f=>{
  f.w.markPageDirty();f.setConfirm(false);f.w.showPage('conf');assert.equal(f.w.eval('CURRENT_PAGE'),'prov');assert(f.w.eval('PAGE_DIRTY'));
  f.setConfirm(true);f.w.showPage('conf');assert.equal(f.w.eval('CURRENT_PAGE'),'conf');
 });
 await test('new_gateway_limit_is_20_and_final_rows_are_safe_dom_handlers',f=>{
  for(let i=0;i<30;i++)f.w.addGateway();assert.equal(f.w.eval('PROVIDER_DRAFT.chain.length'),20);assert(f.get('gateway-add').disabled);
  assert.equal(f.get('chain-list').querySelectorAll('[onclick],[onchange]').length,0);
 });
 await test('hostile_gateway_name_is_rendered_as_text_not_html_or_javascript',f=>{
  const name='<img src=x onerror=alert(1)>';f.get('gateway-name').value=name;f.get('gateway-name').dispatchEvent(new f.w.Event('input',{bubbles:true}));
  assert(f.get('chain-list').textContent.includes(name));assert.equal(f.get('chain-list').querySelectorAll('img,script').length,0);assert.equal(f.get('chain-list').querySelectorAll('[onclick]').length,0);
 });
 await test('invalid_gateway_ids_are_not_accepted_as_dom_or_mutation_targets',f=>{
  const before=f.w.eval('PROVIDER_DRAFT.active');f.w.selectGateway("gw_bad' onclick='alert(1)");f.w.toggleProv('__proto__',{checked:true});assert.equal(f.w.eval('PROVIDER_DRAFT.active'),before);
 });
 await test('overview_shows_today_and_total_cny_separate_historical_usd_and_dynamic_names',async f=>{
  await f.w.loadOverview();const stats=f.get('stats').textContent;assert(stats.includes('≈¥1.25'));assert(stats.includes('≈¥23.50'));assert(stats.includes('≈$0.700'));
  assert(!stats.includes('今日 fal'));assert(!stats.includes('≈¥24.20'));assert(f.get('chain-line').textContent.includes('主网关 → 备用网关'));
  assert(f.get('prov-chips').textContent.includes('备用网关 · 已停用'));assert(!f.get('prov-chips').textContent.includes('本地引擎'));
 });
 await test('overview_escapes_names_and_missing_cost_does_not_become_fabricated_zero',async f=>{
  f.setOverview({stats:{today_cost_cny:0},chain:['worldcodes'],providers:{worldcodes:{name:'<img src=x onerror=alert(1)>',enabled:true,configured:true}},maintenance:{enabled:false}});
  await f.w.loadOverview();assert.equal(f.get('prov-chips').querySelectorAll('img').length,0);assert(f.get('stats').textContent.includes('累计中转网关参考成本'));assert(f.get('stats').textContent.includes('—'));
 });
 await test('job_provider_name_snapshot_wins_then_current_name_then_historical_id',async f=>{
  const job=(id,extra)=>({id,status:'succeeded',created_at:1,quality:'light',...extra});
  f.setJobs([job('one',{provider:'worldcodes',provider_name:'旧名称快照'}),job('two',{provider:secondary}),job('three',{provider:'fal'})]);await f.w.loadJobs();
  const text=f.get('jobs-table').textContent;assert(text.includes('旧名称快照'));assert(text.includes('备用网关'));assert(text.includes('fal'));
 });
 await test('transport_defaults_and_per_tier_override_do_not_couple_paths',async f=>{
  assert.equal(f.get('wc-light-mode').value,'async');f.get('wc-light-mode').value='sync';f.get('wc-fine-mode').value='async';f.get('wc-fine-async').value='/api/images/Edit.php';
  await f.w.saveCurrentPage();const tiers=f.calls[0].providers.worldcodes.tiers;assert.equal(tiers.light.request_mode,'sync');assert.equal(tiers.fine.request_mode,'async');assert.equal(tiers.fine.async_endpoint,'/api/images/Edit.php');assert.equal(tiers.light.async_endpoint,'');
 });
 await test('invalid_numeric_draft_is_retained_across_switch_and_no_put',async f=>{
  f.get('wc-light-timeout').value='';f.w.selectGateway(secondary);f.w.selectGateway('worldcodes');assert.equal(f.get('wc-light-timeout').value,'');await f.w.saveCurrentPage();assert.equal(f.calls.length,0);assert(f.get('toast').textContent.includes('10–600'));
 });
 await test('processing_hint_describes_real_two_transport_modes_without_legacy_mode_claims',f=>{
  f.w.showPage('conf');const text=f.get('processing-mode-summary').textContent;assert(text.includes('云端异步或后台 OpenAI 兼容'));assert(text.includes('图片归一化'));assert(!text.includes('固定使用云端异步'));
 });
 fs.mkdirSync(out,{recursive:true});fs.writeFileSync(path.join(out,'multiple_gateway_admin_results.json'),JSON.stringify({cases:rows},null,2),'utf8');
 const passed=rows.filter(x=>x.passed).length;console.log(`MULTIPLE_GATEWAY_ADMIN_SUMMARY total=${rows.length} passed=${passed} failed=${rows.length-passed}`);process.exitCode=passed===rows.length?0:1;
})().catch(error=>{console.error(error);process.exitCode=1});
