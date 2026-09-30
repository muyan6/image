/* Cloud mode cannot silently fall back to uploading pictures through the VM. */
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const root=path.resolve(process.env.REVIEW_ROOT||path.resolve(__dirname,'../..'));
const out=path.resolve(process.env.REVIEW_OUTPUT||path.join(root,'audit/cloud-frontend-tests'));
fs.mkdirSync(out,{recursive:true});const rows=[];
function api(wx){const module={exports:{}};vm.runInNewContext(fs.readFileSync(path.join(root,'miniprogram/utils/api.js'),'utf8'),{module,exports:module.exports,wx,getApp:()=>({globalData:{apiBase:'https://server.invalid'},setBalance(){}}),console,setTimeout});return module.exports;}
async function test(name,fn){try{await fn();rows.push({case:name,passed:true});}catch(e){console.error(name,e);rows.push({case:name,passed:false});}}
(async()=>{
 await test('cloud_config_outage_does_not_upload_to_vm',async()=>{
  let uploads=0;const a=api({getStorageSync:()=> 'token',request:o=>o.fail({errMsg:'timeout'}),uploadFile:()=>uploads++});
  await assert.rejects(a.submitJob('photo.jpg',{}));assert.equal(uploads,0);
 });
 await test('cloud_missing_cos_does_not_upload_to_vm',async()=>{
  let uploads=0;const a=api({getStorageSync:()=> 'token',request:o=>o.success({statusCode:200,data:{cloud_pipeline:{enabled:true},cos_ready:false}}),uploadFile:()=>uploads++});
  await assert.rejects(a.submitJob('photo.jpg',{}),/COS/);assert.equal(uploads,0);
 });
 await test('cloud_failed_put_does_not_fallback',async()=>{
  let uploads=0,puts=0;const a=api({getStorageSync:()=> 'token',getFileSystemManager:()=>({statSync:()=>({size:10}),readFile:o=>o.success({data:new ArrayBuffer(10)})}),
   request:o=>{if(o.method==='PUT'){puts++;o.fail({errMsg:'COS timeout'});return;}
    o.success({statusCode:200,data:o.url.endsWith('/api/config')?{cloud_pipeline:{enabled:true},cos_ready:true}:{upload_id:'fixture',url:'https://cos.invalid/incoming/fixture'}});},uploadFile:()=>uploads++});
  await assert.rejects(a.submitJob('photo.jpg',{}));assert.equal(puts,1);assert.equal(uploads,0);
 });
 await test('cloud_job_polling_has_no_extra_completion_display_delay',async()=>{
  let now=0;
  const module={exports:{}};
  vm.runInNewContext(fs.readFileSync(path.join(root,'miniprogram/utils/api.js'),'utf8'),
   {module,wx:{getStorageSync:()=> 'fixture',request:o=>o.success({statusCode:200,data:{cloud_pipeline:true,
     stage:'enhance',status:now>0?'succeeded':'processing'}})},getApp:()=>({globalData:{apiBase:'https://server.invalid'}}),
    console,Date:{now:()=>now},setTimeout:(fn,ms)=>{now+=ms;queueMicrotask(fn);}});
  await module.exports.waitForJob('fixture');assert.equal(now,1500);
  const main=fs.readFileSync(path.join(root,'backend/main.py'),'utf8');assert(main.includes('"cloud_pipeline":bool(job.get(\'cloud_pipeline\'))'));
 });
 fs.writeFileSync(path.join(out,'cloud_frontend_results.json'),JSON.stringify({cases:rows},null,2));
 const failed=rows.filter(x=>!x.passed).length;console.log(`CLOUD_FRONTEND_SUMMARY total=${rows.length} passed=${rows.length-failed} failed=${failed}`);process.exitCode=failed?1:0;
})();
