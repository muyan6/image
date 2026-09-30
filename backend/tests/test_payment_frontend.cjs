const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const root=path.resolve(process.env.REVIEW_ROOT||path.resolve(__dirname,'../..'));const out=path.resolve(process.env.REVIEW_OUTPUT||path.join(root,'audit/payment-frontend'));
fs.mkdirSync(out,{recursive:true});const rows=[];
function setup(options={}){
 let stored=null,calls=[],sdk=0;
 const wx={getStorageSync:()=>stored,setStorageSync:(k,v)=>stored=v,removeStorageSync:()=>stored=null,
  getSystemInfoSync:()=>({platform:'android',version:'8.0.70',system:'Android 14'}),
  showModal:o=>o.success({confirm:true}),login:o=>o.success({code:'fixture-code'}),
  requestVirtualPayment:o=>{sdk++;options.cancel?o.fail({errCode:-2,errMsg:'cancel'}):o.success({errMsg:'ok'});},...options.wx};
 const api={ensureLogin:async()=>{},request:async(url,o)=>{calls.push({url,...o});
  if(options.failCreate&&url==='/api/payment/orders'){const e=new Error('network');e.code='NETWORK';throw e;}
  if(url==='/api/payment/orders')return {order:{id:'Pfixture'},pay_data:{mode:'short_series_goods',signData:'{"fixture":true}',paySig:'fixture',signature:'fixture'}};
  return {items:[],order:{id:'Pfixture',status:'created'},balance:100};}};
 const module={exports:{}};const context={module,exports:module.exports,require:()=>api,wx,console,Date,Math,Promise};
 vm.runInNewContext(fs.readFileSync(path.join(root,'miniprogram/utils/payment.js'),'utf8'),context);
 return {p:module.exports,api,wx,calls,sdk:()=>sdk,storage:()=>stored};
}
async function test(name,f){try{await f();rows.push({case:name,passed:true});}catch(e){console.error(name,e);rows.push({case:name,passed:false});}}
(async()=>{
 const pkg={id:'points_600',points:600,price_text:'¥6'};
 await test('pay_success_only_requests_server_confirmation_never_local_credit',async()=>{
  const t=setup();const r=await t.p.buy(pkg);assert.equal(t.sdk(),1);assert.equal(r.orderId,'Pfixture');
  assert.equal(t.calls[0].data.package_id,'points_600');assert(!('amount'in t.calls[0].data));assert(!('points'in t.calls[0].data));
  assert(t.calls.some(x=>x.url.endsWith('/sync')));assert.equal(t.storage(),null);
 });
 await test('pay_cancel_never_retries_sdk_or_credits',async()=>{
  const t=setup({cancel:true});await assert.rejects(t.p.buy(pkg));assert.equal(t.sdk(),1);assert.equal(t.calls.length,1);
 });
 await test('pay_unknown_create_blocks_repeat_order_until_recovered',async()=>{
  const t=setup({failCreate:true});await assert.rejects(t.p.buy(pkg));await assert.rejects(t.p.buy(pkg));
  assert.equal(t.calls.length,1);assert.equal(t.sdk(),0);assert(t.storage());await t.p.recover();assert.equal(t.storage(),null);
 });
 await test('pay_confirm_cancel_does_not_create_order',async()=>{
  const t=setup({wx:{showModal:o=>o.success({confirm:false})}});const r=await t.p.buy(pkg);assert(r.canceled);assert.equal(t.calls.length,0);
 });
 await test('pay_devtools_never_calls_native_payment',async()=>{
  const t=setup({wx:{getSystemInfoSync:()=>({platform:'devtools'})}});await assert.rejects(t.p.buy(pkg));assert.equal(t.sdk(),0);assert.equal(t.calls.length,0);
 });
 await test('pay_old_ios_is_blocked_before_order_creation',async()=>{
  const t=setup({wx:{getSystemInfoSync:()=>({platform:'ios',system:'iOS 14.0',version:'8.0.60'})}});await assert.rejects(t.p.buy(pkg));assert.equal(t.calls.length,0);
 });
 await test('pay_supported_ios_uses_same_virtual_sdk_and_discloses_apple_refund',async()=>{
  let content='';const t=setup({wx:{getSystemInfoSync:()=>({platform:'ios',system:'iOS 15.0',version:'8.0.68'}),
    showModal:o=>{content=o.content;o.success({confirm:true});}}});
  await t.p.buy({...pkg,amount_fen:600});assert.equal(t.sdk(),1);
  assert(content.includes('官方虚拟支付'));assert(content.includes('Apple'));assert(content.includes('App Store'));
  assert(t.calls.some(x=>x.url.endsWith('/sync')));assert.equal(t.storage(),null);
 });
 await test('pay_ios_below_one_yuan_never_creates_order_or_calls_sdk',async()=>{
  const t=setup({wx:{getSystemInfoSync:()=>({platform:'ios',system:'iOS 18.0',version:'8.0.70'})}});
  await assert.rejects(t.p.buy({...pkg,amount_fen:99}),/最低金额为 1 元/);assert.equal(t.calls.length,0);assert.equal(t.sdk(),0);
 });
 await test('pay_android_small_offer_stays_available_and_discloses_refund',async()=>{
  let content='';const t=setup({wx:{showModal:o=>{content=o.content;o.success({confirm:true});}}});
  await t.p.buy({...pkg,amount_fen:99});assert.equal(t.sdk(),1);
  assert(content.includes('官方虚拟支付'));assert(content.includes('联系客服'));assert(!content.includes('App Store'));
 });
 await test('pay_personal_rules_visible_and_admin_cannot_select_sandbox',()=>{
  const xml=fs.readFileSync(path.join(root,'miniprogram/pages/credits/credits.wxml'),'utf8');
  for(const term of ['10 万元','1%','12%','T+3','45–60 天','App Store','不另加收'])assert(xml.includes(term),term);
  const admin=fs.readFileSync(path.join(root,'backend/admin.html'),'utf8');
  const select=admin.match(/<select id="pay-env"[^>]*>([\s\S]*?)<\/select>/);assert(select);assert(!select[1].includes('value="1"'));
  assert(admin.includes('env:0'));assert(admin.includes('开启苹果 IAP'));assert(!admin.includes('id="pay-sandbox-key"'));
 });
 await test('pay_order_center_path_exists_and_is_registered',()=>{
  const app=JSON.parse(fs.readFileSync(path.join(root,'miniprogram/app.json')));assert(app.pages.includes('pages/orders/orders'));
  for(const ext of ['js','json','wxml','wxss'])assert(fs.existsSync(path.join(root,'miniprogram/pages/orders/orders.'+ext)));
  assert(fs.readFileSync(path.join(root,'miniprogram/pages/credits/credits.wxml'),'utf8').includes('onGoOrders'));
 });
 await test('pay_order_entry_is_styled_package_footer_not_unstyled_menu',()=>{
  const xml=fs.readFileSync(path.join(root,'miniprogram/pages/credits/credits.wxml'),'utf8');
  const css=fs.readFileSync(path.join(root,'miniprogram/pages/credits/credits.wxss'),'utf8');
  assert.equal((xml.match(/bindtap="onGoOrders"/g)||[]).length,1);
  assert(xml.indexOf('class="orders-entry"')>xml.indexOf('</block>'));
  assert(xml.indexOf('class="orders-entry"')<xml.indexOf('<!-- 3.'));
  assert(!xml.includes('menu-item'));assert(xml.includes('查看充值记录 · 核对到账'));
  for(const name of ['orders-entry','orders-entry-content','orders-entry-title','orders-entry-desc','orders-entry-arrow','orders-icon'])
   assert(new RegExp('\\.'+name+'\\s*\\{').test(css),name);
  assert(xml.includes('hover-class="orders-entry-pressed"'));
  assert(/\.orders-entry\s*\{[^}]*min-height:\s*112rpx/.test(css));
 });
 await test('pay_order_entry_navigation_does_not_trigger_purchase',()=>{
  let page,navigated,requests=0;
  vm.runInNewContext(fs.readFileSync(path.join(root,'miniprogram/pages/credits/credits.js'),'utf8'),{
   getApp:()=>({globalData:{lightPoints:120},setBalance(){}}),Page:p=>page=p,
   require:()=>({buy:()=>{requests++;},request:()=>{requests++;}}),wx:{navigateTo:o=>navigated=o.url},console});
  page.onGoOrders();assert.equal(navigated,'/pages/orders/orders');assert.equal(requests,0);
 });
 await test('pay_credits_has_busy_guard_and_no_optimistic_balance_increment',()=>{
  const s=fs.readFileSync(path.join(root,'miniprogram/pages/credits/credits.js'),'utf8');assert(s.includes('if(this.data.paymentBusy)return'));
  const handler=s.slice(s.indexOf('async onSelectPackage'),s.indexOf('onGoOrders'));assert(!handler.includes('app.setBalance'));
 });
 fs.writeFileSync(path.join(out,'payment_frontend_results.json'),JSON.stringify({cases:rows},null,2));
 const n=rows.filter(x=>!x.passed).length;console.log(`PAYMENT_FRONTEND_SUMMARY total=${rows.length} passed=${rows.length-n} failed=${n}`);process.exitCode=n?1:0;
})();
