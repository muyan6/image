/* Execute browser direct upload and mini-program approval using isolated stubs. */
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const root=path.resolve(process.env.REVIEW_ROOT||path.resolve(__dirname,'../..'));
const out=path.resolve(process.env.REVIEW_OUTPUT||path.join(root,'audit/platform-frontend-tests'));
fs.mkdirSync(out,{recursive:true});const rows=[];
async function test(name,fn){try{await fn();rows.push({case:name,passed:true});}catch(e){console.error(name,e);rows.push({case:name,passed:false});}}
const {fixture,photo}=require('./test_web_creation_parity.cjs');
(async()=>{
 await test('web_upload_bytes_only_go_to_cos',async()=>{
  const t=fixture(),session=t.session();await session.submit(photo(),40);
  const put=t.calls.find(x=>x.cos&&x.opts.method==='PUT');assert(put.cos.startsWith('https://cos.invalid'));
  assert(!put.opts.headers.Authorization);assert.equal(put.opts.credentials,'omit');
  assert(!t.calls.some(x=>x.url==='/api/rescue'));
  const submitted=t.calls.find(x=>x.url==='/api/rescue/by-upload');assert.equal(submitted.opts.data.upload_id,'ticket');
 });
 await test('web_does_not_submit_when_cos_upload_fails',async()=>{
  const t=fixture({fetch:async()=>({ok:false,status:503})}),session=t.session();
  await assert.rejects(session.submit(photo(),40),/直传/);
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
