/* User-experience integration with offline DOM/network/storage fixtures only. */
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const root=path.resolve(process.env.REVIEW_ROOT||path.resolve(__dirname,'../..'));
const out=path.resolve(process.env.REVIEW_OUTPUT||path.join(root,'audit/experience-web'));
fs.mkdirSync(out,{recursive:true});const rows=[],tick=()=>new Promise(r=>setImmediate(r));
const response=(data,status=200)=>({ok:status>=200&&status<300,status,json:async()=>data,blob:async()=>({})});
function element(){return {style:{},children:[],classList:{add(){},remove(){}},appendChild(x){this.children.push(x);},click(){},files:[]};}
function web(options={}){
  const nodes={},storage=options.storage||new Map(),calls=[],timers=new Map();let seq=0;
  const get=id=>nodes[id]||(nodes[id]=element());
  const document={getElementById:get,createElement:element,body:element(),hidden:false,addEventListener(n,f){this[n]=f;}};
  const context={document,window:{},console:{log(){},warn(){},error(){}},Date,URL:{createObjectURL:()=> 'blob:photo',revokeObjectURL(){}},
    localStorage:{getItem:k=>storage.get(k)||'',setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
    setTimeout:(fn,ms)=>{timers.set(++seq,{fn,ms});return seq;},clearTimeout:id=>timers.delete(id),setInterval:()=>1,clearInterval(){},alert:s=>calls.push({alert:s}),
    fetch:async(url,opts={})=>{
      calls.push({url,opts});const custom=options.fetch&&await options.fetch(url,opts);if(custom)return custom;
      if(url==='/api/auth/web')return response({token:'fixture-session',balance:200});
      if(url==='/api/me')return response({user_id:options.owner||'fixture-user',balance:200});
      if(url==='/api/config')return response({prices:{light:40,fine:60},free_mode:false});
      if(url.startsWith('/api/my/jobs?'))return response({jobs:[],has_more:false,next_offset:0,total:0});
      if(url==='/api/uploads')return response({upload_id:'fixture-upload',url:'https://cos.invalid/original'});
      if(url.startsWith('https://cos.invalid/'))return response({});
      if(url.endsWith('/complete'))return response({ok:true});
      if(url==='/api/rescue/by-upload')return response({code:0,job_id:'fixture-job',balance:140});
      if(url.startsWith('/api/jobs/'))return response({id:'fixture-job',status:'succeeded',balance:140,orig_url:'https://cos.invalid/original',result_url:'https://cos.invalid/result'});
      throw new Error('Unexpected fixture URL '+url);
    }};
  vm.createContext(context);const script=[...fs.readFileSync(path.join(root,'backend/index.html'),'utf8').matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)][0][1];vm.runInContext(script,context);
  return {nodes,storage,calls,timers,document,run:s=>vm.runInContext(s,context)};
}
async function test(name,fn){try{await fn();rows.push({case:name,passed:true});console.log('PASS '+name);}catch(e){rows.push({case:name,passed:false,error:String(e.stack)});console.error('FAIL '+name,e);}}
(async()=>{
  await test('web_quote_is_visible_and_submission_carries_expected_price_and_durable_id',async()=>{
    const t=web();await tick();assert(t.nodes.webQuote.textContent.includes('60'));
    t.run("handleFile({name:'fixture.jpg',size:4,type:'image/jpeg'})");await t.nodes.btnRescue.onclick();
    const body=JSON.parse(t.calls.find(x=>x.url==='/api/rescue/by-upload').opts.body);
    assert.equal(body.expected_price,60);assert.match(body.client_request_id,/^[A-Za-z0-9_-]{8,80}$/);
    assert.equal(t.calls.filter(x=>x.url==='/api/rescue/by-upload').length,1);
  });
  await test('web_price_change_requires_a_second_explicit_confirmation',async()=>{
    let price=60;const t=web({fetch:url=>url==='/api/config'?response({prices:{light:40,fine:price}}):null});await tick();
    t.run("handleFile({name:'fixture.jpg',size:4,type:'image/jpeg'})");price=80;await t.nodes.btnRescue.onclick();
    assert(!t.calls.some(x=>x.url==='/api/uploads'));assert(t.nodes.webQuote.textContent.includes('80'));
    await t.nodes.btnRescue.onclick();assert.equal(t.calls.filter(x=>x.url==='/api/rescue/by-upload').length,1);
  });
  await test('web_failed_job_uses_returned_balance_and_actual_refund',async()=>{
    const t=web({fetch:url=>url.startsWith('/api/jobs/')?response({status:'failed',error:'fixture failure',balance:200,charged_amount:60,refunded_amount:60}):null});await tick();
    t.run("handleFile({name:'fixture.jpg',size:4,type:'image/jpeg'})");await t.nodes.btnRescue.onclick();
    assert(t.nodes.balanceBadge.textContent.includes('200'));assert(t.calls.some(x=>x.alert&&x.alert.includes('退回 60')));
  });
  await test('web_recovers_a_lost_response_after_reopening_without_billable_replay',async()=>{
    const storage=new Map([['pr_pending_submission_v1',JSON.stringify({owner:'fixture-user',client_request_id:'client_fixture',uncertain:true,upload:{upload_id:'fixture-upload'}})]]);
    const t=web({storage,fetch:url=>url.startsWith('/api/me/submissions/')?response({state:'accepted',job_id:'existing-job',status:'processing'}):null});await tick();await tick();
    assert.equal(t.run('pendingWebJob'),'existing-job');assert.equal(t.run('webSubmissionUncertain'),false);
    assert(!t.calls.some(x=>x.url==='/api/rescue/by-upload'||x.url==='/api/uploads'));
  });
  await test('web_uncertain_receipt_remains_guarded_and_owner_scoped',async()=>{
    const storage=new Map([['pr_pending_submission_v1',JSON.stringify({owner:'fixture-user',client_request_id:'client_fixture',uncertain:true})]]);
    const t=web({storage,fetch:url=>url.startsWith('/api/me/submissions/')?response({state:'uncertain',job_id:null},202):null});await tick();await tick();
    assert.equal(t.run('webSubmissionUncertain'),true);t.run("handleFile({name:'new.jpg',size:5})");assert.equal(t.run('selectedFile'),null);
    const other=web({storage,owner:'different-user'});await tick();assert.equal(other.run('webSubmissionUncertain'),false);
    assert(!other.calls.some(x=>x.url&&x.url.startsWith('/api/me/submissions/')));
  });
  await test('web_works_paginates_on_demand_without_duplicates',async()=>{
    const t=web({fetch:url=>url.startsWith('/api/my/jobs?')?response({jobs:url.includes('offset=0')?[{id:'one',status:'succeeded',result_url:'https://cos.invalid/one'}]:[{id:'one',status:'succeeded',result_url:'https://cos.invalid/one'},{id:'two',status:'succeeded',result_url:'https://cos.invalid/two'}],has_more:url.includes('offset=0'),next_offset:url.includes('offset=0')?1:3}):null});await tick();
    assert.equal(t.calls.filter(x=>x.url&&x.url.startsWith('/api/my/jobs?')).length,1);
    await t.nodes.webMoreWorks.onclick();assert.equal(t.run('webWorks.length'),2);assert.equal(t.nodes.webMoreWorks.hidden,true);
  });
  await test('web_does_not_submit_before_account_identity_is_verified',async()=>{
    const t=web({fetch:url=>url==='/api/me'?response({balance:200}):null});await tick();
    t.run("handleFile({name:'fixture.jpg',size:4,type:'image/jpeg'})");await t.nodes.btnRescue.onclick();
    assert(!t.calls.some(x=>x.url==='/api/uploads'||x.url==='/api/rescue/by-upload'));
    assert(!t.storage.has('pr_pending_submission_v1'));
  });
  await test('web_definite_missing_receipt_retries_the_frozen_id_and_upload_only',async()=>{
    const storage=new Map([['pr_pending_submission_v1',JSON.stringify({owner:'fixture-user',client_request_id:'original_request',uncertain:true,quality:'light',upload:{upload_id:'original-upload'},payload:{quality:'light',expected_price:40}})]]);
    const t=web({storage,fetch:url=>url.startsWith('/api/me/submissions/')?response({state:'not_found',detail:'not registered'},404):null});await tick();await tick();
    assert.equal(t.run('webSubmissionUncertain'),false);await t.nodes.btnRescue.onclick();
    const submission=t.calls.find(x=>x.url==='/api/rescue/by-upload'),body=JSON.parse(submission.opts.body);
    assert.equal(body.client_request_id,'original_request');assert.equal(body.upload_id,'original-upload');assert.equal(body.quality,'light');assert.equal(body.expected_price,40);
    assert(!t.calls.some(x=>x.url==='/api/uploads'));
  });
  await test('web_repeated_storage_failure_never_reaches_billable_post',async()=>{
    const t=web();await tick();t.run("localStorage.setItem=()=>{throw new Error('fixture storage full');};handleFile({name:'fixture.jpg',size:4,type:'image/jpeg'})");
    await t.nodes.btnRescue.onclick();await t.nodes.btnRescue.onclick();
    assert(!t.calls.some(x=>x.url==='/api/rescue/by-upload'));
  });
  await test('late_previous_account_preview_cannot_replace_new_account_view',async()=>{
    let finish;const t=web({fetch:url=>url==='/api/jobs/old-account-job'?new Promise(r=>finish=r):null});await tick();
    const old=t.run("openWebWork('old-account-job')");await tick();t.run('webWorksGeneration++;clearWebAccountView()');
    finish(response({status:'succeeded',orig_url:'https://cos.invalid/old',result_url:'https://cos.invalid/old-result'}));await old;
    assert.equal(t.nodes.resImg.src,'');assert.equal(t.nodes.btnDownload.href,'');assert.equal(t.nodes.workspace.style.display,'none');
  });
  await test('web_poll_timer_stops_when_hidden',async()=>{
    const t=web({fetch:url=>url.startsWith('/api/my/jobs?')?response({jobs:[{id:'one',status:'processing',stage:'enhance',created_at:Date.now()/1000-10}],total:1,next_offset:1}):null});await tick();
    assert(t.timers.size);t.document.hidden=true;t.document.visibilitychange();assert.equal(t.timers.size,0);
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
