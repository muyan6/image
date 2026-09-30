/* Execute browser direct upload and mini-program approval using isolated stubs. */
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const root=path.resolve(process.env.REVIEW_ROOT||path.resolve(__dirname,'../..'));
const out=path.resolve(process.env.REVIEW_OUTPUT||path.join(root,'audit/platform-frontend-tests'));
fs.mkdirSync(out,{recursive:true});const rows=[];
async function test(name,fn){try{await fn();rows.push({case:name,passed:true});}catch(e){console.error(name,e);rows.push({case:name,passed:false});}}
const html=fs.readFileSync(path.join(root,'backend/index.html'),'utf8');
const script=[...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)].map(x=>x[1]).join('\n');
function node(){return {style:{},classList:{add(){},remove(){},toggle(){}},addEventListener(){},appendChild(){},click(){},getBoundingClientRect(){return {left:0,width:100};}};}
function setup(){
 const nodes=new Map(),get=id=>{if(!nodes.has(id))nodes.set(id,node());return nodes.get(id);};const calls=[];
 const response=(data,status=200)=>({ok:status>=200&&status<300,status,json:async()=>data,blob:async()=>({})});
 const c={document:{getElementById:get,createElement:node,body:node()},console,localStorage:{getItem:()=>'',setItem(){},removeItem(){}},
  URL:{createObjectURL:()=> 'blob:fixture',revokeObjectURL(){}},FileReader:function(){},Image:function(){},
  setInterval:()=>1,clearInterval(){},setTimeout,alert(){},
  fetch:async(url,opts={})=>{calls.push({url,opts});
   if(url==='/api/auth/web')return response({token:'fixture-token',balance:100});
   if(url==='/api/uploads')return response({upload_id:'owned-upload',url:'https://cos.invalid/owned.jpg'});
   if(url.startsWith('https://cos.invalid'))return response({});
   if(url.endsWith('/complete'))return response({ok:true});
   if(url==='/api/rescue/by-upload')return response({code:0,job_id:'fixture'});
   throw Error('Unexpected URL '+url);
  }};c.window=c;vm.createContext(c);vm.runInContext(script,c);return {c,calls};
}
(async()=>{
 await test('web_upload_bytes_only_go_to_cos',async()=>{
  const t=setup();await vm.runInContext("submitWebPicture({name:'fixture.jpg',size:123,type:'image/jpeg'},'light')",t.c);
  const put=t.calls.find(x=>x.opts.method==='PUT');assert(put.url.startsWith('https://cos.invalid'));
  assert(!t.calls.some(x=>x.url==='/api/rescue'));
  const submit=t.calls.find(x=>x.url==='/api/rescue/by-upload');assert.equal(JSON.parse(submit.opts.body).upload_id,'owned-upload');
 });
 await test('web_does_not_submit_when_cos_upload_fails',async()=>{
  const t=setup();const fetch=t.c.fetch;t.c.fetch=async(url,opts)=>url.startsWith('https://cos.invalid')?{ok:false}:fetch(url,opts);
  await assert.rejects(vm.runInContext("submitWebPicture({name:'fixture.jpg',size:123},'light')",t.c),/COS/);
  assert(!t.calls.some(x=>x.url==='/api/rescue/by-upload'));
 });
 await test('mini_program_approval_is_explicit_and_uses_authenticated_request',async()=>{
  let page;const calls=[];const api={ensureLogin:async()=>{},request:async(p,o)=>calls.push({p,o})};
  const c={require:()=>api,Page:p=>page=p,decodeURIComponent};vm.createContext(c);
  vm.runInContext(fs.readFileSync(path.join(root,'miniprogram/pages/web-login/web-login.js'),'utf8'),c);
  page.setData=function(x){Object.assign(this.data,x);};page.onLoad({scene:'a'.repeat(32)});
  assert.equal(calls.length,0);await page.choose('approve');
  assert.equal(calls.length,1);assert.equal(calls[0].p,'/api/auth/wechat-web/approve');assert.equal(calls[0].o.data.action,'approve');
  await page.choose('approve');assert.equal(calls.length,1);
 });
 await test('mini_program_invalid_scene_cannot_approve',async()=>{
  let page;const api={ensureLogin:async()=>{throw Error('unexpected');},request:async()=>{throw Error('unexpected');}};
  const c={require:()=>api,Page:p=>page=p,decodeURIComponent};vm.createContext(c);
  vm.runInContext(fs.readFileSync(path.join(root,'miniprogram/pages/web-login/web-login.js'),'utf8'),c);
  page.setData=function(x){Object.assign(this.data,x);};page.onLoad({scene:'invalid'});assert(page.data.done);
  await page.choose('approve');
 });
 const failed=rows.filter(x=>!x.passed).length;fs.writeFileSync(path.join(out,'platform_frontend_results.json'),JSON.stringify({cases:rows},null,2));
 console.log(`PLATFORM_FRONTEND_SUMMARY total=${rows.length} passed=${rows.length-failed} failed=${failed}`);process.exitCode=failed?1:0;
})();
