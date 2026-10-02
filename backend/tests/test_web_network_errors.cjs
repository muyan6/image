/* Network failures in production COS upload, recipe download and result download. */
const assert=require('assert');
const fs=require('fs');
const path=require('path');
const {fixture,Element,photo,tick}=require('./test_web_creation_parity.cjs');
const root=path.resolve(__dirname,'../..');
const output=path.resolve(process.env.REVIEW_OUTPUT||path.join(root,'audit/web-network-errors'));
const rows=[];
const networkError=async()=>{throw new TypeError('Failed to fetch');};
async function test(name,fn){try{await fn();rows.push({case:name,passed:true});console.log('PASS '+name);}catch(e){rows.push({case:name,passed:false,error:String(e.stack)});console.error('FAIL '+name,e.message);}}
(async()=>{
 await test('cos_put_rejection_has_chinese_phase_message_and_never_submits',async()=>{
  const f=fixture({fetch:networkError}),s=f.session();
  await assert.rejects(s.submit(photo(),40),e=>e.message==='照片直传连接失败，请检查网络后重试');
  assert(f.calls.some(c=>c.cos&&c.opts.method==='PUT'));
  assert(!f.calls.some(c=>c.url==='/api/rescue/by-upload'));
  assert.equal(s.receipt(),null);s.close();
 });
 await test('raw_original_rejection_has_chinese_phase_message',async()=>{
  const f=fixture({fetch:networkError}),s=f.session();
  await assert.rejects(s.original({complete:true,job_id:'job',orig_url:'https://cos.invalid/raw-original'}),e=>e.message==='原图下载连接失败，请检查网络后重试');
  assert.equal(f.calls.filter(c=>c.cos).length,1);s.close();
 });
 await test('result_download_rejection_keeps_visible_result_and_retry_actions',async()=>{
  const f=fixture({fetch:networkError}),view=new Element('main');
  const close=await f.production.mountCreation(f.ctx,view,'result',{jobId:'job'});
  try{
   await tick();assert.equal(view.querySelectorAll('img').length,2);
   await view.querySelectorAll('button').find(b=>b.textContent==='下载图片').click();
   assert.equal(f.messages.at(-1),'图片下载连接失败，请检查网络后重试');
   assert.equal(view.querySelectorAll('img').length,2);
   assert(view.querySelectorAll('button').some(b=>b.textContent==='下载图片'));
   assert(view.querySelectorAll('button').some(b=>b.textContent==='高清预览'));
  }finally{close();}
 });
 await test('cos_put_http_rejection_preserves_status_and_does_not_post_generation',async()=>{
  const f=fixture({fetch:async()=>({ok:false,status:403})}),s=f.session();
  await assert.rejects(s.submit(photo(),40),e=>e.status===403&&e.message==='照片直传未完成，请重试');
  assert(!f.calls.some(c=>c.url==='/api/rescue/by-upload'));s.close();
 });
 await test('raw_original_missing_preserves_retention_status',async()=>{
  const f=fixture({fetch:async()=>({ok:false,status:404})}),s=f.session();
  await assert.rejects(s.original({complete:true,job_id:'job',orig_url:'https://cos.invalid/raw-original'}),e=>e.status===404&&e.message==='原图已到保存期限，请重新选择照片');s.close();
 });
 fs.mkdirSync(output,{recursive:true});fs.writeFileSync(path.join(output,'web_network_error_results.json'),JSON.stringify({cases:rows},null,2));
 const failed=rows.filter(r=>!r.passed).length;
 console.log(`WEB_NETWORK_ERRORS_SUMMARY total=${rows.length} passed=${rows.length-failed} failed=${failed}`);
 process.exitCode=failed?1:0;
})().catch(e=>{console.error(e.stack);process.exitCode=1;});