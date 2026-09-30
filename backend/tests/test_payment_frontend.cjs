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
 await test('pay_order_center_path_exists_and_is_registered',()=>{
  const app=JSON.parse(fs.readFileSync(path.join(root,'miniprogram/app.json')));assert(app.pages.includes('pages/orders/orders'));
  for(const ext of ['js','json','wxml','wxss'])assert(fs.existsSync(path.join(root,'miniprogram/pages/orders/orders.'+ext)));
  assert(fs.readFileSync(path.join(root,'miniprogram/pages/credits/credits.wxml'),'utf8').includes('onGoOrders'));
 });
 await test('pay_credits_has_busy_guard_and_no_optimistic_balance_increment',()=>{
  const s=fs.readFileSync(path.join(root,'miniprogram/pages/credits/credits.js'),'utf8');assert(s.includes('if(this.data.paymentBusy)return'));
  const handler=s.slice(s.indexOf('async onSelectPackage'),s.indexOf('onGoOrders'));assert(!handler.includes('app.setBalance'));
 });
 fs.writeFileSync(path.join(out,'payment_frontend_results.json'),JSON.stringify({cases:rows},null,2));
 const n=rows.filter(x=>!x.passed).length;console.log(`PAYMENT_FRONTEND_SUMMARY total=${rows.length} passed=${rows.length-n} failed=${n}`);process.exitCode=n?1:0;
})();
