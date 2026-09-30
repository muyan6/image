/* UI-only regressions. Every wx/API action is mocked; no live requests or payments. */
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const root=path.resolve(process.env.REVIEW_ROOT||path.resolve(__dirname,'../..'));
const out=path.resolve(process.env.REVIEW_OUTPUT||path.join(root,'audit/frontend-polish-tests'));
fs.mkdirSync(out,{recursive:true});
const rows=[],tick=()=>new Promise(r=>setImmediate(r));
const xml=name=>fs.readFileSync(path.join(root,`miniprogram/pages/${name}/${name}.wxml`),'utf8');
function page(name,api={},wx={},payment={}){
 let p;const app={globalData:{historyList:[],freeMode:false,mediaCache:{}},persist(){},setBalance(){}};
 vm.runInNewContext(fs.readFileSync(path.join(root,`miniprogram/pages/${name}/${name}.js`),'utf8'),{
  Page:x=>p=x,getApp:()=>app,require:n=>n.includes('/commerce')?require(path.join(root,'miniprogram/utils/commerce.js')):n.includes('/payment')?payment:api,
  wx:{getStorageSync:()=>null,setStorageSync(){},showToast(){},showModal(){},showLoading(){},hideLoading(){},vibrateShort(){},...wx},
  console:{log(){},warn(){},error(){}},setTimeout,clearTimeout});
 p.data=JSON.parse(JSON.stringify(p.data));p.setData=d=>Object.assign(p.data,d);return p;
}
async function test(name,fn){try{await fn();rows.push({case:name,passed:true});console.log('PASS '+name);}catch(e){rows.push({case:name,passed:false,error:String(e.stack)});console.error('FAIL '+name,e);}}
(async()=>{
 await test('template_error_is_not_empty_and_retry_recovers',async()=>{
  let fail=true;const p=page('templates',{config:async()=>({}),absolute:x=>x,templates:async()=>{if(fail)throw new Error('offline');return {items:[],groups:[]};}});
  await new Promise(resolve=>p.fetchTemplates(resolve));assert(p.data.loadError);assert(!p.data.loading);
  assert(xml('templates').includes('!loadError && loaded'));
  fail=false;p.onRetryTemplates();await tick();assert.equal(p.data.loadError,'');assert(p.data.loaded);assert.equal(p.data.filteredTemplates.length,0);
 });
 await test('template_cached_cards_survive_network_failure',async()=>{
  const p=page('templates',{config:async()=>({}),absolute:x=>x,templates:async()=>{throw new Error('offline');}},
    {getStorageSync:()=>({groups:[],items:[{id:'cached',name:'模板'}]})});
  await new Promise(resolve=>p.fetchTemplates(resolve));assert(p.data.loadError);assert.equal(p.data.filteredTemplates[0].id,'cached');
 });
 await test('orders_error_and_empty_are_mutually_exclusive',async()=>{
  const p=page('orders',{request:async()=>{throw new Error('offline');}}, {}, {recover:async()=>{}});
  p._active=true;await p.loadOrders();assert.equal(p.data.error,'offline');assert(!p.data.busy);assert(!p.data.loaded);
  const s=xml('orders');assert(s.includes('wx:if="{{error}}"'));assert(s.includes('wx:elif="{{loaded && !items.length}}"'));
 });
 await test('orders_success_uses_status_tone_and_copy_does_not_pay',async()=>{
  const calls=[];let copied;const p=page('orders',{request:async url=>{calls.push(url);return {balance:600,items:[{id:'order123',points:600,amount:600,status:'delivered',created_at:1000,refunded_fen:0}]};}},
    {setClipboardData:o=>copied=o.data},{recover:async()=>{}});
  p._active=true;await p.loadOrders();assert(p.data.loaded);assert.equal(p.data.items[0].statusTone,'success');assert.equal(p.data.items[0].priceText,'¥6.00');
  p.onCopyOrder({currentTarget:{dataset:{id:'order123'}}});assert.equal(copied,'order123');assert.deepEqual(calls,['/api/payment/orders']);
  assert(!xml('orders').includes('核对到账 / 退款'));
 });
 await test('text_config_error_differs_from_disabled_and_retries',async()=>{
  let fail=true;const p=page('text-generation',{config:async()=>{if(fail)throw new Error('offline');return {text_generation:{ready:true,price:75}};}});
  await p.onShow();assert(p.data.configError);assert(!p.data.ready);assert(!p.data.configLoading);
  fail=false;await p.loadConfig();assert.equal(p.data.configError,'');assert(p.data.ready);assert.equal(p.data.price,75);
 });
 await test('text_examples_preserve_drafts_until_confirmed_and_respect_busy',()=>{
  let modal;const p=page('text-generation',{}, {showModal:o=>modal=o});const event={currentTarget:{dataset:{index:0}}};
  p.data.prompt='我的描述';p.onUseExample(event);assert.equal(p.data.prompt,'我的描述');modal.success({confirm:false});assert.equal(p.data.prompt,'我的描述');
  p.onUseExample(event);modal.success({confirm:true});assert.equal(p.data.prompt,p.data.examples[0].prompt);
  p.data.busy=true;p.onClearPrompt();assert(p.data.prompt);p.data.busy=false;p.onClearPrompt();assert.equal(p.data.prompt,'');
 });
 await test('late_text_config_does_not_overwrite_newer_result',async()=>{
  const resolves=[];const p=page('text-generation',{config:()=>new Promise(r=>resolves.push(r))});
  const first=p.loadConfig(),second=p.loadConfig();resolves[1]({text_generation:{ready:true,price:80}});await second;
  resolves[0]({text_generation:{ready:false,price:40}});await first;assert(p.data.ready);assert.equal(p.data.price,80);
 });
 await test('works_filter_keeps_original_indices_and_updates_counts',()=>{
  const p=page('works');p.setWorks([{jobId:'a',status:'processing'},{jobId:'b',status:'failed'},{jobId:'c',status:'succeeded',result:'c'}]);
  p.onFilterStatus({currentTarget:{dataset:{id:'succeeded'}}});assert.equal(p.data.filteredWorks[0].sourceIndex,2);
  assert.equal(p.data.workFilters.find(f=>f.id==='all').count,3);
  p.setWorks([{jobId:'a',status:'succeeded',result:'a'},{jobId:'b',status:'failed'}]);assert.equal(p.data.filteredWorks[0].jobId,'a');assert.equal(p.data.filteredWorks[0].sourceIndex,0);
 });
 await test('works_photo_opens_preview_and_more_opens_menu',async()=>{
  let preview,menus=0;const p=page('works',{downloadJobMedia:async()=> 'wxfile://result.jpg'},
    {previewImage:o=>preview=o.current,showActionSheet:()=>menus++});
  p._visible=true;p.setWorks([{jobId:'a',status:'succeeded',result:'https://cos.invalid/a'}]);p.refreshWork=async x=>x;
  p.onTapWork({currentTarget:{dataset:{index:0}}});await tick();assert.equal(preview,'wxfile://result.jpg');assert.equal(menus,0);
  p.onMoreWork({currentTarget:{dataset:{index:0}}});assert.equal(menus,1);
  assert(xml('works').includes('catchtap="onMoreWork"'));assert(xml('works').includes('data-index="{{ item.sourceIndex }}"'));
 });
 await test('works_filter_empty_is_distinct_from_no_works',()=>{
  const p=page('works');p.setWorks([{jobId:'a',status:'succeeded',result:'a'}]);p.onFilterStatus({currentTarget:{dataset:{id:'failed'}}});
  assert.equal(p.data.filteredWorks.length,0);assert.equal(p.data.works.length,1);assert(xml('works').includes('当前筛选下暂无作品'));
 });
 await test('output_help_collapses_without_changing_mode_or_price',()=>{
  const p=page('adjust');const before=JSON.stringify([p.data.templateOutputMode,p.data.quality,p.data.currentQualityCost]);
  assert(!p.data.showOutputHelp);p.onToggleOutputHelp();assert(p.data.showOutputHelp);p.onToggleOutputHelp();
  assert.equal(JSON.stringify([p.data.templateOutputMode,p.data.quality,p.data.currentQualityCost]),before);
  assert(!xml('adjust').includes('15~25'));assert(xml('adjust').includes('output-diagram-pair'));
 });
 await test('generation_status_displays_reported_stage_without_fixed_eta',async()=>{
  const observed=[];let p;
  p=page('adjust',{absolute:x=>x,submitJob:async()=>({code:0,job_id:'fixture'}),waitForJob:async(id,options)=>{
    for(const stage of ['queued','normalize','enhance','store_cos','finalize']){options.onTick({stage});observed.push(p.data.processingText);}
    const e=new Error('background');e.code='USER_BACKGROUND';throw e;
  }});
  p._foreground=true;await p.executeUpload('fixture.jpg');
  assert.deepEqual(observed,['任务排队中…','正在准备照片…','正在生成图片…','正在保存图片…','正在整理生成结果…']);
 });
 await test('theme_markup_handlers_and_styles_are_consistent',()=>{
  for(const name of ['orders','text-generation','templates','works','adjust']){
    const p=page(name),s=xml(name);
    for(const m of s.matchAll(/\b(?:bind|catch)\w+="([a-zA-Z_$][\w$]*)"/g))if(!['true','false'].includes(m[1]))assert.equal(typeof p[m[1]],'function',name+':'+m[1]);
    const css=fs.readFileSync(path.join(root,`miniprogram/pages/${name}/${name}.wxss`),'utf8');assert.equal((css.match(/\{/g)||[]).length,(css.match(/\}/g)||[]).length);
  }
  const css=fs.readFileSync(path.join(root,'miniprogram/pages/compare/compare.wxss'),'utf8');assert(css.includes('env(safe-area-inset-bottom)'));
  assert(!xml('compare').includes('EXPORT MASTER'));assert(!xml('templates').includes('微米级'));
 });
 const failed=rows.filter(r=>!r.passed).length;
 fs.writeFileSync(path.join(out,'frontend_polish_results.json'),JSON.stringify({cases:rows},null,2),'utf8');
 console.log(`FRONTEND_POLISH_SUMMARY total=${rows.length} passed=${rows.length-failed} failed=${failed}`);process.exitCode=failed?1:0;
})();
