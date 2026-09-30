/* Admin forms and the unmodified mini-program payment adapter, with fake DOM/HTTP only. */
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const root=path.resolve(process.env.REVIEW_ROOT||path.resolve(__dirname,'../..'));
const out=path.resolve(process.env.REVIEW_OUTPUT||path.join(root,'audit/commerce-admin-tests'));
const html=fs.readFileSync(path.join(root,'backend/admin.html'),'utf8');
const script=[...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)].map(m=>m[1]).join('\n');
const defaultPackage={id:'points_600',product_id:'points_600',enabled:true,amount_fen:600,points:600,description:'',
 promotion:{enabled:false,product_id:'points_600',amount_fen:600,points:600,description:'',starts_at:0,ends_at:0}};
const decode=s=>s.replace(/&quot;/g,'"').replace(/&#39;/g,"'").replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&amp;/g,'&');
function setup(){
 const nodes=new Map(),calls=[];let settings={commerce:{welcome_points:100,packages:[JSON.parse(JSON.stringify(defaultPackage))]}};
 class E{
  constructor(id){this.id=id;this.value='';this.checked=false;this.disabled=false;this.hidden=false;this.dataset={};this.children=[];this.classList={add(){},remove(){},toggle(){},contains:()=>false};}
  set value(v){this._value=String(v??'');}get value(){return this._value;}
  appendChild(n){this.children.push(n);return n;}
  querySelectorAll(){return [...nodes.values()].filter(n=>n.id.startsWith('cp-')||n.id==='commerce-welcome');}
  set innerHTML(s){this.html=s;parse(s);}get innerHTML(){return this.html||'';}
  insertAdjacentHTML(position,s){this.html=(this.html||'')+s;parse(s);}
  addEventListener(){}
 }
 const get=id=>{if(!nodes.has(id))nodes.set(id,new E(id));return nodes.get(id);};
 function parse(s){for(const m of s.matchAll(/<input\b([^>]*)>/g)){const id=m[1].match(/id="([^"]+)"/);if(!id)continue;const e=get(id[1]);e.value=decode(m[1].match(/value="([^"]*)"/)?.[1]||'');e.checked=/\bchecked\b/.test(m[1]);}}
 const document={getElementById:get,createElement:()=>new E('generated'),querySelector:()=>null,querySelectorAll:()=>[],addEventListener(){}};
 const ctx={document,console,setTimeout:()=>0,clearTimeout(){},setInterval:()=>0,confirm:()=>true,location:{origin:'https://fixture.invalid'},fetch:async(url,opt={})=>{
  if(url.endsWith('/session'))return {ok:false,status:401,json:async()=>({})};
  const body=opt.body?JSON.parse(opt.body):null;calls.push({url,body});if(body)settings={...settings,...body};
  return {ok:true,status:200,json:async()=>settings};
 }};ctx.window=ctx;vm.createContext(ctx);vm.runInContext(script,ctx);vm.runInContext('SETTINGS='+JSON.stringify(settings),ctx);ctx.showPage('commerce');
 return {ctx,get,calls};
}
const rows=[];async function test(name,fn){try{await fn();rows.push({case:name,passed:true});}catch(e){console.error(name,e);rows.push({case:name,passed:false,error:e.message});}}
(async()=>{
 await test('commerce_has_one_central_save_and_prefills_defaults',()=>{
  const t=setup();assert.equal(t.get('commerce-welcome').value,'100');assert.equal(t.get('cp-0-price').value,'6.00');
  assert(!t.get('page-save-bar').hidden);assert.equal((html.match(/id="page-save"/g)||[]).length,1);
  assert(!html.includes('onclick="saveCommerce('));
 });
 await test('welcome_zero_and_fractional_price_save_only_commerce',async()=>{
  const t=setup();t.get('commerce-welcome').value='0';t.get('cp-0-price').value='4.80';t.get('cp-0-points').value='800';
  await t.ctx.saveCurrentPage();assert.equal(t.calls.length,1);assert.deepEqual(Object.keys(t.calls[0].body),['commerce']);
  const c=t.calls[0].body.commerce;assert.equal(c.welcome_points,0);assert.equal(c.packages[0].amount_fen,480);assert.equal(c.packages[0].points,800);
 });
 await test('activity_times_always_use_beijing_timezone',async()=>{
  const t=setup();t.get('cp-0-sale').checked=true;t.get('cp-0-sale-product').value='holiday_600';t.get('cp-0-sale-price').value='4.8';
  t.get('cp-0-start').value='2026-10-01T00:00:00';t.get('cp-0-end').value='2026-10-08T00:00:00';await t.ctx.saveCurrentPage();
  const sale=t.calls[0].body.commerce.packages[0].promotion;
  assert.equal(sale.starts_at,Date.UTC(2026,8,30,16)/1000);assert.equal(sale.ends_at,Date.UTC(2026,9,7,16)/1000);
  assert.equal(t.ctx.commerceTime(sale.starts_at),'2026-10-01T00:00:00');
 });
 await test('invalid_money_does_not_submit_or_destroy_draft',async()=>{
  const t=setup();t.get('cp-0-price').value='4.805';t.ctx.markPageDirty();await t.ctx.saveCurrentPage();
  assert.equal(t.calls.length,0);assert.equal(t.get('cp-0-price').value,'4.805');assert(vm.runInContext('PAGE_DIRTY',t.ctx));
 });
 await test('add_package_preserves_unsaved_existing_fields',()=>{
  const t=setup();t.get('cp-0-price').value='4.80';t.ctx.addCommercePackage();
  assert.equal(t.get('cp-0-price').value,'4.80');assert.equal(t.ctx.readCommerce().packages.length,2);assert(!t.get('cp-1-enabled').checked);
 });
 await test('different_sale_price_requires_separate_wechat_product',async()=>{
  const t=setup();t.get('cp-0-sale').checked=true;t.get('cp-0-sale-price').value='4.8';
  t.get('cp-0-start').value='2026-10-01T00:00:00';t.get('cp-0-end').value='2026-10-08T00:00:00';
  await t.ctx.saveCurrentPage();assert.equal(t.calls.length,0);
 });
 await test('released_payment_adapter_forwards_opaque_offer_without_client_price',async()=>{
  let sent,sdk,modal;const module={exports:{}};
  const api={ensureLogin:async()=>{},request:async(url,opts)=>{
   if(url==='/api/payment/orders'){sent=opts.data;return {order:{id:'Pfixture'},pay_data:{signData:'{"goodsPrice":480}',paySig:'fixture',signature:'fixture',mode:'short_series_goods'}};}
   return {};
  }};
  const wx={getStorageSync:()=>null,setStorageSync(){},removeStorageSync(){},getSystemInfoSync:()=>({platform:'android',version:'8.0.70'}),
   showModal:o=>{modal=o.content;o.success({confirm:true});},login:o=>o.success({code:'fixture'}),requestVirtualPayment:o=>{sdk=o;o.success({});}};
  vm.runInNewContext(fs.readFileSync(path.join(root,'miniprogram/utils/payment.js'),'utf8'),{module,exports:module.exports,require:()=>api,wx,console});
  const id='points_600__opaque_offer_version';await module.exports.buy({id,points:800,price_text:'¥4.8'});
  assert.equal(sent.package_id,id);assert(!('amount'in sent));assert(!('points'in sent));assert(modal.includes('¥4.8'));assert.equal(JSON.parse(sdk.signData).goodsPrice,480);
 });
 fs.mkdirSync(out,{recursive:true});fs.writeFileSync(path.join(out,'commerce_admin_results.json'),JSON.stringify({cases:rows},null,2),'utf8');
 const failed=rows.filter(r=>!r.passed).length;console.log(`COMMERCE_ADMIN_SUMMARY total=${rows.length} passed=${rows.length-failed} failed=${failed}`);process.exitCode=failed?1:0;
})();
