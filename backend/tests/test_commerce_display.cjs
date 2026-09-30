/* Execute the actual page modules using an isolated wx clock/network double. */
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const root=path.resolve(process.env.REVIEW_ROOT||path.resolve(__dirname,'../..'));
const out=path.resolve(process.env.REVIEW_OUTPUT||path.join(root,'audit/commerce-display'));
const commerce=require(path.join(root,'miniprogram/utils/commerce.js')),rows=[];
const tick=()=>new Promise(r=>setImmediate(r));
function page(name,{api={},payment={},wx={},now=100000}={}){
 let p,clock=now,serial=0;const timers=new Map(),messages=[];
 const app={globalData:{lightPoints:100,historyList:[],mediaCache:{},freeMode:false},persist(){},setBalance(n){this.globalData.lightPoints=n;}};
 const mocks={showToast:o=>messages.push(o),showModal:o=>messages.push(o),navigateTo(){},showLoading(){},hideLoading(){},getFileInfo:o=>o.success({size:1}),...wx};
 const DateMock=class extends Date{static now(){return clock;}};
 vm.runInNewContext(fs.readFileSync(path.join(root,`miniprogram/pages/${name}/${name}.js`),'utf8'),{
  Page:x=>p=x,getApp:()=>app,require:s=>s.includes('commerce.js')?commerce:s.includes('payment.js')?payment:api,wx:mocks,console,
  Date:DateMock,setTimeout:(fn,ms)=>{const id=++serial;timers.set(id,{fn,at:clock+ms});return id;},clearTimeout:id=>timers.delete(id)});
 p.data=JSON.parse(JSON.stringify(p.data));p.setData=d=>Object.assign(p.data,d);
 return {p,app,messages,timers,async advance(ms){clock+=ms;const due=[...timers].filter(([,v])=>v.at<=clock);for(const [id,v] of due){timers.delete(id);v.fn();}await tick();},setClock:n=>clock=n};
}
const sale={id:'offer-sale',points:800,generations:20,price_text:'¥4.8',amount_fen:480,regular_amount_fen:600,regular_price_text:'¥6',promotion_active:true,promotion_ends_at:102,bonus_text:'国庆加赠'};
const regular={id:'offer-normal',points:600,price_text:'¥6',amount_fen:600,promotion_active:false};
const catalog=(packages=[sale],time=100,next=102)=>({packages,server_time:time,next_change_at:next,payment_ready:true,generation_cost:40});
async function test(name,fn){try{await fn();rows.push({case:name,passed:true});}catch(e){console.error(name,e);rows.push({case:name,passed:false,error:e.stack});}}
(async()=>{
 await test('strike_price_only_when_actual_discount_is_active',()=>{
  assert.equal(commerce.decorate([sale],100)[0].originalPrice,'¥6');
  assert.equal(commerce.decorate([{...sale,amount_fen:600}],100)[0].originalPrice,'');
  assert.equal(commerce.decorate([sale],102)[0].originalPrice,'');
  assert.equal(commerce.decorate([regular],100)[0].originalPrice,'');
 });
 await test('equal_price_bonus_has_distinct_label',()=>assert.equal(commerce.decorate([{...sale,amount_fen:600}],100)[0].promotionLabel,'限时加赠'));
 await test('countdown_days_and_zero_are_bounded',()=>{
  assert.equal(commerce.countdown(90061),'1天 01:01:01');assert.equal(commerce.countdown(-1),'00:00:00');
 });
 await test('server_clock_corrects_wrong_device_clock',async()=>{
  const {p}=page('credits',{now:999999999,api:{request:async()=>catalog()}});p._visible=true;await p.refreshCatalog();
  assert.equal(p.data.packages[0].countdownText,'00:00:02');p.onHide();
 });
 await test('expiry_refreshes_normal_offer_without_local_price_calculation',async()=>{
  let calls=0;const t=page('credits',{api:{request:async()=>++calls===1?catalog():catalog([regular],102,0)}});
  t.p._visible=true;await t.p.refreshCatalog();await t.advance(2000);
  assert.equal(calls,2);assert.equal(t.p.data.packages[0].id,'offer-normal');assert(t.p.data.paymentReady);assert.equal(t.timers.size,0);
 });
 await test('future_activity_start_reloads_catalog',async()=>{
  let calls=0;const t=page('credits',{api:{request:async()=>++calls===1?catalog([regular],100,101):catalog([sale],101,102)}});
  t.p._visible=true;await t.p.refreshCatalog();assert(!t.p.data.packages[0].promotionVisible);await t.advance(1000);
  assert(t.p.data.packages[0].promotionVisible);t.p.onHide();
 });
 await test('expiry_network_failure_blocks_payment_and_retries_with_backoff',async()=>{
  let calls=0;const t=page('credits',{api:{request:async()=>{if(++calls===1)return catalog();throw new Error('offline');}}});
  t.p._visible=true;await t.p.refreshCatalog();await t.advance(2000);
  assert(!t.p.data.paymentReady);assert(t.p.data.catalogError);assert(!t.p.data.packages[0].promotionVisible);
  await t.advance(1000);assert.equal(calls,2);await t.advance(14000);assert.equal(calls,3);t.p.onHide();
 });
 await test('hide_and_unload_stop_timers_and_late_updates',async()=>{
  const t=page('credits',{api:{request:async()=>catalog()}});t.p._visible=true;await t.p.refreshCatalog();
  assert.equal(t.timers.size,1);t.p.onHide();assert.equal(t.timers.size,0);
  let resolve;const u=page('credits',{api:{request:()=>new Promise(r=>resolve=r)}});const pending=u.p.refreshCatalog();u.p.onUnload();resolve(catalog());await pending;
  assert.equal(u.p.data.packages.length,0);assert.equal(u.timers.size,0);
 });
 await test('older_catalog_remains_compatible_no_fake_promotions',async()=>{
  const t=page('credits',{api:{request:async()=>({packages:[regular],payment_ready:true,generation_cost:40})}});await t.p.refreshCatalog();
  assert(t.p.data.paymentReady);assert(!t.p.data.packages[0].promotionVisible);assert.equal(t.timers.size,0);
 });
 await test('purchase_rechecks_changed_offer_and_never_calls_payment',async()=>{
  let buys=0;const t=page('credits',{api:{request:async()=>catalog([regular],103,0)},payment:{buy:async()=>{buys++;return {};}}});
  t.p.data.packages=[sale];t.p.data.paymentReady=true;await t.p.onSelectPackage({currentTarget:{dataset:{id:sale.id}}});
  assert.equal(buys,0);assert(t.messages.some(m=>m.title.includes('更新')));
 });
 await test('valid_purchase_calls_existing_payment_flow_once',async()=>{
  let buys=0;const t=page('credits',{api:{request:async()=>catalog()},payment:{buy:async p=>{buys++;assert.equal(p.id,sale.id);return {};}}});
  t.p.data.packages=[sale];t.p.data.paymentReady=true;
  await Promise.all([t.p.onSelectPackage({currentTarget:{dataset:{id:sale.id}}}),t.p.onSelectPackage({currentTarget:{dataset:{id:sale.id}}})]);
  assert.equal(buys,1);t.p.onHide();
 });
 await test('failed_initial_catalog_has_no_hardcoded_buyable_price',async()=>{
  const t=page('credits',{api:{request:async()=>{throw new Error('offline');}}});await t.p.refreshCatalog();
  assert.equal(t.p.data.packages.length,0);assert(!t.p.data.paymentReady);t.p.onHide();
 });
 await test('delete_dialog_freezes_target_before_refresh',async()=>{
  let confirm,deleted;const a={jobId:'a'},b={jobId:'b'};
  const t=page('works',{api:{deleteJob:async id=>{deleted=id;return {};},myJobs:async()=>({jobs:[]})},wx:{showModal:o=>confirm=o.success}});
  t.p.data.works=[a,b];t.app.globalData.historyList=[a,b];t.p.deleteSingleWork(0);t.p.data.works=[b,a];await confirm({confirm:true});assert.equal(deleted,'a');
 });
 await test('all_pages_kept_before_authoritative_merge',async()=>{
  const jobs=Array.from({length:101},(_,i)=>({id:String(i),status:'failed'}));let calls=0;
  const t=page('works',{api:{absolute:x=>x,myJobs:async(limit,offset=0)=>{calls++;return {jobs:jobs.slice(offset,offset+limit),has_more:offset===0,next_offset:offset+limit};}}});
  await t.p.loadWorks();assert.equal(t.p.data.works.length,101);assert.equal(calls,2);
 });
 await test('generation_requires_loaded_price',async()=>{
  let submits=0;const t=page('adjust',{api:{config:async()=>{throw new Error('offline');},me:async()=>({balance:100})}});
  t.p.executeUpload=()=>submits++;await t.p.onStartGenerate();await tick();assert.equal(submits,0);assert(!t.p.data.priceReady);
 });
 await test('async_violation_feedback_uses_record_id',async()=>{
  let modal,payload;const t=page('works',{api:{submitViolationFeedback:async(id,text)=>{payload={id,text};}},wx:{showModal:o=>modal=o.success}});
  const item={violation:{violation_id:'job-fixture',feedback_submitted:false}};t.p.submitWorkFeedback(item);await modal({confirm:true,content:'请复核'});
  assert.deepEqual(payload,{id:'job-fixture',text:'请复核'});assert(item.violation.feedback_submitted);
 });
 await test('theme_uses_existing_tokens_and_conditional_strike',()=>{
  const css=fs.readFileSync(path.join(root,'miniprogram/pages/credits/credits.wxss'),'utf8'),xml=fs.readFileSync(path.join(root,'miniprogram/pages/credits/credits.wxml'),'utf8');
  assert(css.includes('var(--accent-gold)'));assert(css.includes('var(--paper-card-subtle)'));assert(css.includes('text-decoration: line-through'));
  assert(xml.includes('wx:if="{{ item.originalPrice }}"'));assert(xml.includes('item.countdownText'));assert(!css.includes('animation:'));
 });
 fs.mkdirSync(out,{recursive:true});fs.writeFileSync(path.join(out,'commerce_display_results.json'),JSON.stringify({cases:rows},null,2));
 const failed=rows.filter(r=>!r.passed).length;console.log(`COMMERCE_DISPLAY_SUMMARY total=${rows.length} passed=${rows.length-failed} failed=${failed}`);process.exitCode=failed?1:0;
})();
