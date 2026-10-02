/* Browser experience regressions migrated to the real creation module. */
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const {fixture,text,photo,tick}=require('./test_web_creation_parity.cjs');
const root=path.resolve(process.env.REVIEW_ROOT||path.resolve(__dirname,'../..'));
const out=path.resolve(process.env.REVIEW_OUTPUT||path.join(root,'audit/experience-web'));
fs.mkdirSync(out,{recursive:true});const rows=[];
async function test(name,fn){try{await fn();rows.push({case:name,passed:true});console.log('PASS '+name);}catch(e){rows.push({case:name,passed:false,error:String(e.stack)});console.error('FAIL '+name,e);}}
(async()=>{
  await test('registered_web_submission_carries_visible_price_and_durable_id',async()=>{
    const f=fixture(),s=f.session();await s.submit(text(),40);const body=f.calls.find(c=>c.url==='/api/text-generation').opts.data;
    assert.equal(body.expected_price,40);assert.match(body.client_request_id,/^wc_[A-Za-z0-9_-]{8,70}$/);assert(s.receipt().job_id);
  });
  await test('web_price_change_requires_second_explicit_confirmation',async()=>{
    const f=fixture(),s=f.session();f.config.text_generation.price=80;await assert.rejects(s.submit(text(),40),e=>e.code==='PRICE_CHANGED');
    assert(!f.calls.some(c=>c.url==='/api/text-generation'));await s.submit(text(),80);assert.equal(f.calls.filter(c=>c.url==='/api/text-generation').length,1);
  });
  await test('guest_browsing_never_registers_or_starts_a_billable_request',async()=>{
    const f=fixture({guest:true}),s=f.session();await s.catalog();assert.equal(await s.submit(text(),40),null);
    assert(!f.calls.some(c=>/register|earn|text-generation/.test(c.url||'')));assert.equal(f.ctx.state.user,null);
  });
  await test('web_zero_balance_is_not_automatically_replenished',async()=>{
    const f=fixture(),s=f.session();f.ctx.state.user.balance=0;await assert.rejects(s.submit(text(),40),e=>e.status===402);
    assert(!f.calls.some(c=>c.opts&&c.opts.method==='POST'));assert.equal(f.ctx.state.user.balance,0);
  });
  await test('accepted_receipt_survives_remount_and_prevents_reupload',async()=>{
    const f=fixture(),s=f.session();await s.submit(photo(),40);s.close();const next=f.session();await next.submit(photo(),40);
    assert.equal(f.calls.filter(c=>c.url==='/api/rescue/by-upload').length,1);assert.equal(f.calls.filter(c=>c.url==='/api/uploads').length,1);
  });
  await test('storage_failures_on_repeated_clicks_never_bill',async()=>{
    const f=fixture(),s=f.session();f.failStorage();for(let i=0;i<2;i++)await assert.rejects(s.submit(text(),40),/回执未保存/);
    assert(!f.calls.some(c=>c.url==='/api/text-generation'));
  });
  await test('site_identity_is_stable_across_wechat_shared_owner_changes',async()=>{
    const f=fixture();f.ctx.state.user.account_user_id='SITE_stable';const s=f.session();await s.submit(text(),40);s.close();
    f.ctx.state.user.user_id='WX_shared';f.ctx.accountVersion++;const next=f.session();await next.submit(text(),40);
    assert.equal(f.calls.filter(c=>c.url==='/api/text-generation').length,1);assert.equal(next.owner,'SITE_stable');
  });
  await test('cleanup_prevents_late_personal_metadata_from_modifying_state',async()=>{
    const f=fixture(),s=f.session();let resolve;f.ctx.api=()=>new Promise(r=>resolve=r);const pending=s.catalog();s.close();resolve({items:[{id:'late'}]});
    await assert.rejects(pending,e=>e.code==='INACTIVE');assert.equal(s.templates.length,0);
  });
  await test('home_tracks_background_completion_without_truncating_history',async()=>{
    const history=[{jobId:'old',status:'succeeded'},{jobId:'done',status:'processing'}],app={globalData:{historyList:history},persist(){}};let p;
    const api={myJobs:async()=>({jobs:[],processing_count:0,total:0}),request:async()=>({status:'succeeded',result_url:'https://cos.invalid/done'}),absolute:x=>x};
    vm.runInNewContext(fs.readFileSync(path.join(root,'miniprogram/pages/index/index.js'),'utf8'),{getApp:()=>app,require:n=>n.includes('creation-draft')?{}:api,Page:x=>p=x,wx:{navigateTo(){}},console,setTimeout,clearTimeout,Date});
    p.data=JSON.parse(JSON.stringify(p.data));p.setData=d=>Object.assign(p.data,d);p._visible=true;await p.loadPendingTasks();
    assert.equal(app.globalData.historyList.length,2);assert.equal(app.globalData.historyList[1].status,'succeeded');assert.equal(p.data.finishedJobId,'done');assert.equal(p.data.pendingCount,0);
  });
  await test('api_list_status_and_request_id_are_transmitted_and_raw_original_renews_recipe',async()=>{
    const requests=[],module={exports:{}},app={globalData:{apiBase:'https://server.invalid'},setBalance(){}};let downloads=0;
    const wx={getStorageSync:()=> 'token',getImageInfo:o=>o.success({width:100,height:100}),getFileSystemManager:()=>({statSync:()=>({size:4}),readFile:o=>o.success({data:new ArrayBuffer(4)})}),
      downloadFile:o=>{requests.push(o.url);if(++downloads===1)o.success({statusCode:403});else o.success({statusCode:200,tempFilePath:'wxfile://complete-original'});},
      request:o=>{requests.push(o.url);const data=o.url.endsWith('/api/config')?{cos_ready:true}:o.url.endsWith('/recipe')?{orig_url:'https://cos.invalid/full-new'}:o.url.endsWith('/api/uploads')?{upload_id:'upload',url:'https://cos.invalid/upload'}:{code:0,job_id:'created'};if(o.url.includes('/api/rescue/by-upload'))assert.equal(o.data.client_request_id,'client_fixture');o.success({statusCode:200,data});}};
    vm.runInNewContext(fs.readFileSync(path.join(root,'miniprogram/utils/api.js'),'utf8'),{module,wx,getApp:()=>app,console,setTimeout,Date});
    await module.exports.myJobs(24,0,'processing');await module.exports.submitJob('fixture.jpg',{client_request_id:'client_fixture'});
    assert(requests.some(x=>x.includes('status=processing')));
    assert.equal(await module.exports.downloadRecipeOriginal('owned','https://cos.invalid/full-old'),'wxfile://complete-original');
    assert(requests.includes('https://cos.invalid/full-new'));assert(!requests.some(x=>/comparison/.test(x)));
  });
  const failed=rows.filter(x=>!x.passed).length;fs.writeFileSync(path.join(out,'experience_web_results.json'),JSON.stringify({cases:rows},null,2));
  console.log(`EXPERIENCE_WEB_SUMMARY total=${rows.length} passed=${rows.length-failed} failed=${failed}`);process.exitCode=failed?1:0;
})();
