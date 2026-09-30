/* Offline latency/lifecycle regressions; no network, account, or runtime-store access. */
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const root = path.resolve(process.env.REVIEW_ROOT || path.resolve(__dirname, '../..'));
const out = path.resolve(process.env.REVIEW_OUTPUT || path.join(root, 'audit/review-frontend-latency'));
fs.mkdirSync(out, {recursive:true});
const rows = [], tick = () => new Promise(r => setImmediate(r));
const silent = {log(){}, warn(){}, error(){}};
function app(history=[]) {
  return {globalData:{apiBase:'https://server.invalid',historyList:history,mediaCache:{},lightPoints:200},
    setBalance(n){this.globalData.lightPoints=n;},persist(){},clearHistory(){this.globalData.historyList=[];}};
}
function api(wx, clock={}) {
  const a=app(), module={exports:{}};
  vm.runInNewContext(fs.readFileSync(path.join(root,'miniprogram/utils/api.js'),'utf8'),
    {module,wx,getApp:()=>a,console:silent,setTimeout:clock.setTimeout||setTimeout,Date:clock.Date||Date});
  return module.exports;
}
function page(name, apiMock={}, a=app(), extraWx={}, clock={}) {
  let p;
  const wx={showToast(){},showModal(o){o.success?.({confirm:true});},showLoading(){},hideLoading(){},
    navigateTo(){},redirectTo(){},vibrateShort(){},stopPullDownRefresh(){},getFileInfo:o=>o.success({size:4}),...extraWx};
  vm.runInNewContext(fs.readFileSync(path.join(root,`miniprogram/pages/${name}/${name}.js`),'utf8'),
    {getApp:()=>a,require:()=>apiMock,Page:x=>p=x,wx,console:silent,setTimeout:clock.setTimeout||setTimeout,
      clearTimeout:clock.clearTimeout||clearTimeout,Date});
  p.data=JSON.parse(JSON.stringify(p.data));p.setData=d=>Object.assign(p.data,d);return p;
}
function web(fetchMock, clock={}) {
  const elements={}, el=id=>elements[id]||= {style:{},classList:{add(){},remove(){}},files:[],click(){}};
  const script=[...fs.readFileSync(path.join(root,'backend/index.html'),'utf8').matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)][0][1];
  const alerts=[];
  const context={document:{getElementById:el,createElement:()=>el('login-panel'),body:{appendChild(){}}},window:{},
    console:silent,localStorage:{getItem:()=> 'session',setItem(){},removeItem(){}},
    URL:{createObjectURL:()=> 'blob:fixture',revokeObjectURL(){}},fetch:fetchMock,alert:s=>alerts.push(s),
    setTimeout:clock.setTimeout||setTimeout,clearInterval(){},setInterval(){},Date:clock.Date||Date};
  vm.createContext(context);vm.runInContext(script,context);
  return {elements,alerts,run:s=>vm.runInContext(s,context)};
}
const response=(data,status=200)=>({ok:status>=200&&status<300,status,json:async()=>data});
async function test(name,fn) {
  if(process.env.REVIEW_CASE_PATTERN&&!new RegExp(process.env.REVIEW_CASE_PATTERN).test(name))return;
  try { const observed=await fn();rows.push({case:name,passed:true,observed});console.log('PASS '+name+': '+JSON.stringify(observed||{})); }
  catch(e) { rows.push({case:name,passed:false,error:String(e.stack)});console.log('FAIL '+name+': '+e.message); }
}
(async()=>{
  await test('cloud_completion_display_uses_1500ms_not_5000ms',async()=>{
    let now=0,calls=0;
    const a=api({getStorageSync:()=> 'session',request:o=>{calls++;o.success({statusCode:200,data:
      {status:now>=1?'succeeded':'processing',stage:'enhance',cloud_pipeline:true}});}},
      {Date:{now:()=>now},setTimeout(fn,ms){now+=ms;queueMicrotask(fn);}});
    await a.waitForJob('fixture');assert.equal(now,1500);assert.equal(calls,2);return {completionAtMs:1,displayAtMs:now,requests:calls};
  });
  await test('poll_sleep_respects_remaining_deadline',async()=>{
    let now=0;
    const a=api({getStorageSync:()=> 'session',request:o=>o.success({statusCode:200,data:{status:'processing'}})},
      {Date:{now:()=>now},setTimeout(fn,ms){now+=ms;queueMicrotask(fn);}});
    await assert.rejects(a.waitForJob('fixture',{timeout:1000,interval:5000}),e=>e.code==='TIMEOUT');
    assert.equal(now,1000);return {deadlineMs:1000,stoppedAtMs:now};
  });
  await test('config_coalesces_inflight_only_and_fetches_fresh_after_completion',async()=>{
    let requests=0;const callbacks=[];
    const a=api({getStorageSync:()=> '',request:o=>{requests++;callbacks.push(o);}});
    const first=a.config(),second=a.config();const concurrent=requests;
    callbacks.splice(0).forEach(o=>o.success({statusCode:200,data:{prices:{light:40}}}));await Promise.all([first,second]);
    const third=a.config();callbacks.splice(0).forEach(o=>o.success({statusCode:200,data:{prices:{light:80}}}));
    assert.equal((await third).prices.light,80);assert.equal(concurrent,1);assert.equal(requests,2);
    return {concurrentRequests:concurrent,totalWithFreshConfig:requests};
  });
  await test('login_and_public_config_begin_in_parallel_before_upload',async()=>{
    let token='',loginCallback,configCallback,uploadCalls=0;const events=[];
    const a=api({getStorageSync:()=>token,setStorageSync:(k,v)=>token=v,login:o=>{events.push('login');loginCallback=o;},
      request:o=>{if(o.url.endsWith('/api/config')){events.push('config');configCallback=o;}
        else o.success({statusCode:200,data:{token:'session'}});},
      uploadFile:o=>{uploadCalls++;o.success({statusCode:200,data:JSON.stringify({code:0,job_id:'fixture'})});}});
    const submitted=a.submitJob('fixture.jpg',{});await tick();const parallel=events.includes('config');
    if(configCallback)configCallback.success({statusCode:200,data:{cos_ready:false}});
    loginCallback.success({code:'fixture'});await tick();
    if(!parallel)configCallback.success({statusCode:200,data:{cos_ready:false}});
    await submitted;assert(parallel);assert.equal(uploadCalls,1);return {events,uploads:uploadCalls};
  });
  await test('file_read_overlaps_upload_ticket_roundtrip',async()=>{
    let uploadCallback,readCallback,reading=false;
    const a=api({getStorageSync:()=> 'session',getFileSystemManager:()=>({statSync:()=>({size:4}),readFile:o=>{reading=true;readCallback=o;}}),
      request:o=>{if(o.url.endsWith('/api/config'))o.success({statusCode:200,data:{cos_ready:true}});
        else if(o.url.endsWith('/api/uploads'))uploadCallback=o;
        else o.success({statusCode:200,data:{code:0,job_id:'fixture'}});}});
    const submitted=a.submitJob('fixture.jpg',{});await tick();const overlap=reading;
    uploadCallback.success({statusCode:200,data:{url:'https://cos.invalid/upload',upload_id:'fixture'}});await tick();
    readCallback.success({data:new ArrayBuffer(4)});await submitted;assert(overlap);return {readingBeforeTicketResponse:overlap};
  });
  for(const mode of ['network','http503','invalid200'])await test('multipart_'+mode+'_preserves_submission_uncertainty',async()=>{
    const a=api({getStorageSync:()=> 'session',uploadFile:o=>mode==='network'?o.fail({errMsg:'timeout'}):
      o.success({statusCode:mode==='http503'?503:200,data:mode==='invalid200'?'not-json':JSON.stringify({detail:'temporary'})})});
    let error;try{await a.upload('fixture.jpg',{});}catch(e){error=e;}
    assert(error&&error.jobSubmissionAttempted);return {uncertain:!!error.jobSubmissionAttempted};
  });
  await test('multipart_definitive_rejection_does_not_block_a_corrected_retry',async()=>{
    const a=api({getStorageSync:()=> 'session',uploadFile:o=>o.success({statusCode:402,data:JSON.stringify({detail:'余额不足'})})});
    let error;try{await a.upload('fixture.jpg',{});}catch(e){error=e;}
    assert.equal(error.status,402);assert(!error.jobSubmissionAttempted);return {status:error.status,uncertain:!!error.jobSubmissionAttempted};
  });
  await test('adjust_hide_return_during_upload_never_submits_twice',async()=>{
    let submits=0;const resolvers=[];const a=app();
    const p=page('adjust',{submitJob:()=>{submits++;return new Promise(r=>resolvers.push(r));},absolute:x=>x},a);
    p.data.imagePath='fixture.jpg';p.data.lightPoints=200;
    const first=p.executeUpload('fixture.jpg');p.onHide();p.onShow();await p.onStartGenerate();await tick();
    p.onHide();resolvers.forEach(r=>r({code:0,job_id:'fixture'+submits,orig_url:'https://cos.invalid/orig'}));
    await first;await tick();assert.equal(submits,1);return {submissions:submits,savedJobs:a.globalData.historyList.length};
  });
  await test('adjust_background_submission_failure_keeps_uncertain_guard',async()=>{
    let rejects,submits=0;const error=Object.assign(new Error('timeout'),{jobSubmissionAttempted:true});
    const p=page('adjust',{submitJob:()=>{submits++;return submits===1?new Promise((r,j)=>rejects=j):Promise.reject(error);}});
    p.data.lightPoints=200;p.data.imagePath='fixture.jpg';const first=p.executeUpload('fixture.jpg');p.onHide();rejects(error);await first;
    p.onShow();await p.onStartGenerate();await tick();assert.equal(submits,1);assert(p._submissionUncertain);
    return {submissions:submits,uncertain:!!p._submissionUncertain};
  });
  await test('adjust_unload_before_file_info_stops_new_submission',async()=>{
    let fileInfo,submits=0;
    const p=page('adjust',{submitJob:async()=>{submits++;throw new Error('stop');}},app(),{getFileInfo:o=>fileInfo=o});
    p.data.priceReady=true;p.data.lightPoints=200;p.data.imagePath='fixture.jpg';await p.onStartGenerate();p.onUnload();fileInfo.success({size:4});await tick();
    assert.equal(submits,0);return {submissions:submits};
  });
  await test('work_list_reuses_authoritative_status_without_per_job_refetch',async()=>{
    let lists=0,requests=0;const p=page('works',{myJobs:async()=>{lists++;return {jobs:[{id:'one',status:'processing'},{id:'two',status:'processing'}]};},
      absolute:x=>x,request:async()=>{requests++;return {status:'processing'};}});
    await p.loadWorks();assert.equal(lists,1);assert.equal(requests,0);return {listRequests:lists,individualRequests:requests};
  });
  await test('work_list_ignores_stale_response_after_hide',async()=>{
    let resolve;const a=app();const p=page('works',{myJobs:()=>new Promise(r=>resolve=r),absolute:x=>x},a);
    const pending=p.loadWorks();p.onHide();resolve({jobs:[{id:'late',status:'succeeded',result_url:'https://cos.invalid/result'}]});
    await pending;assert.equal(a.globalData.historyList.length,0);assert.equal(p.data.loading,false);return {lateJobsAdded:a.globalData.historyList.length};
  });
  await test('deleting_one_legacy_work_preserves_other_legacy_works',async()=>{
    const a=app([{result:'one.jpg'},{result:'two.jpg'}]);const p=page('works',{myJobs:async()=>({jobs:[]})},a);
    p.setWorks(a.globalData.historyList);p.deleteSingleWork(0);await tick();await tick();
    assert.equal(a.globalData.historyList.length,1);assert.equal(a.globalData.historyList[0].result,'two.jpg');
    return {remaining:a.globalData.historyList.map(w=>w.result)};
  });
  await test('deleting_cloned_legacy_preview_removes_only_one_matching_record',async()=>{
    const a=app([{result:'one.jpg'},{result:'one.jpg'},{result:'two.jpg'}]);
    const p=page('works',{myJobs:async()=>{throw new Error('offline');}},a);
    await p.loadWorks();p.deleteSingleWork(0);await tick();await tick();
    assert.equal(a.globalData.historyList.length,2);assert.equal(a.globalData.historyList.filter(w=>w.result==='one.jpg').length,1);
    return {remaining:a.globalData.historyList.map(w=>w.result)};
  });
  await test('old_poll_completion_cannot_release_a_new_submission_lock',async()=>{
    let firstWait,secondSubmit;let submits=0;
    const p=page('adjust',{submitJob:async()=>{submits++;return submits===1?{code:0,job_id:'first'}:new Promise(r=>secondSubmit=r);},
      absolute:x=>x,waitForJob:()=>new Promise(r=>firstWait=r)});
    const first=p.executeUpload('fixture.jpg');await tick();p.onHide();p.onShow();
    const second=p.executeUpload('fixture.jpg');firstWait({status:'succeeded'});await first;
    const locked=!!p._submissionPending;p.onHide();secondSubmit({code:0,job_id:'second'});await second;
    assert(locked);return {secondUploadStayedLocked:locked};
  });
  await test('web_double_click_does_not_duplicate_billable_submission',async()=>{
    let uploads=0,accepted;const t=web(async(url)=>{
      if(url==='/api/auth/web')return response({token:'session'});
      if(url==='/api/uploads'){uploads++;return response({url:'https://cos.invalid/upload',upload_id:'fixture'});}
      if(url==='/api/rescue/by-upload')return new Promise(r=>accepted=r);
      if(url.startsWith('/api/jobs/'))return response({status:'succeeded',result_url:'https://cos.invalid/result'});
      return response({});});
    await tick();t.run("handleFile({name:'fixture.jpg',size:4,type:'image/jpeg'})");
    const first=t.elements.btnRescue.onclick();await tick();const second=t.elements.btnRescue.onclick();await tick();
    const count=uploads;accepted(response({code:0,job_id:'fixture'}));
    if(count===1)await Promise.all([first,second]);else await tick();
    assert.equal(count,1);return {uploads:count};
  });
  await test('web_poll_timeout_retry_reuses_accepted_job_without_reupload',async()=>{
    let now=0,uploads=0,done=false;
    const t=web(async(url)=>{
      if(url==='/api/auth/web')return response({token:'session'});
      if(url==='/api/uploads'){uploads++;return response({url:'https://cos.invalid/upload',upload_id:'fixture'});}
      if(url==='/api/rescue/by-upload')return response({code:0,job_id:'fixture'});
      if(url.startsWith('/api/jobs/'))return response({status:done?'succeeded':'processing',result_url:done?'https://cos.invalid/result':''});
      return response({});},{Date:{now:()=>now},setTimeout(fn,ms){now+=ms;queueMicrotask(fn);}});
    await tick();t.run("handleFile({name:'fixture.jpg',size:4})");await t.elements.btnRescue.onclick();
    done=true;await t.elements.btnRescue.onclick();assert.equal(uploads,1);return {uploads,firstTimeoutAtMs:180000};
  });
  await test('web_uncertain_submission_blocks_repeat_after_network_loss',async()=>{
    let submissions=0;
    const t=web(async(url)=>{
      if(url==='/api/auth/web')return response({token:'session'});
      if(url==='/api/uploads')return response({url:'https://cos.invalid/upload',upload_id:'fixture'});
      if(url==='/api/rescue/by-upload'){submissions++;throw new Error('network timeout');}
      return response({});});
    await tick();t.run("handleFile({name:'fixture.jpg',size:4})");await t.elements.btnRescue.onclick();await t.elements.btnRescue.onclick();
    assert.equal(submissions,1);return {submissions};
  });
  for(const stage of ['create','complete'])await test('web_'+stage+'_503_is_retryable_without_billable_uncertainty',async()=>{
    let creates=0,submissions=0;
    const t=web(async(url)=>{
      if(url==='/api/auth/web')return response({token:'session'});
      if(url==='/api/uploads'){creates++;return stage==='create'?response({detail:'temporary'},503):response({url:'https://cos.invalid/upload',upload_id:'fixture'});}
      if(url.endsWith('/complete'))return response({detail:'temporary'},503);
      if(url==='/api/rescue/by-upload'){submissions++;return response({code:0,job_id:'fixture'});}
      return response({});});
    await tick();t.run("handleFile({name:'fixture.jpg',size:4})");await t.elements.btnRescue.onclick();await t.elements.btnRescue.onclick();
    assert.equal(creates,2);assert.equal(submissions,0);return {retryCreates:creates,billableSubmissions:submissions};
  });
  await test('web_missing_job_clears_pending_id_before_explicit_retry',async()=>{
    let creates=0,now=0;
    const t=web(async(url)=>{
      if(url==='/api/auth/web')return response({token:'session'});
      if(url==='/api/uploads'){creates++;return response({url:'https://cos.invalid/upload',upload_id:'fixture'});}
      if(url==='/api/rescue/by-upload')return response({code:0,job_id:'fixture'});
      if(url.startsWith('/api/jobs/'))return creates===1?response({detail:'not found'},404):response({status:'succeeded',result_url:'https://cos.invalid/result'});
      return response({});},{Date:{now:()=>now},setTimeout(fn,ms){now+=ms;queueMicrotask(fn);}});
    await tick();t.run("handleFile({name:'fixture.jpg',size:4})");await t.elements.btnRescue.onclick();await t.elements.btnRescue.onclick();
    assert.equal(creates,2);assert.equal(t.alerts.length,1);return {creates,queryErrors:t.alerts.length};
  });
  await test('all_mini_program_scripts_and_wxml_handlers_parse',()=>{
    let files=0,bindings=0;const walk=d=>fs.readdirSync(d,{withFileTypes:true}).flatMap(e=>e.isDirectory()?walk(path.join(d,e.name)):[path.join(d,e.name)]);
    for(const f of walk(path.join(root,'miniprogram')).filter(f=>f.endsWith('.js'))) {new vm.Script(fs.readFileSync(f,'utf8'));files++;}
    for(const f of walk(path.join(root,'miniprogram/pages')).filter(f=>f.endsWith('.wxml'))) {
      const js=fs.readFileSync(f.replace(/\.wxml$/,'.js'),'utf8');
      for(const m of fs.readFileSync(f,'utf8').matchAll(/(?:bind|catch)(?::)?[a-zA-Z-]+\s*=\s*"([a-zA-Z_$][\w$]*)"/g)) {
        if(m[1]==='true')continue;assert(new RegExp('\\b'+m[1]+'\\s*\\(').test(js),f+': '+m[1]);bindings++;
      }
    }
    const s=[...fs.readFileSync(path.join(root,'backend/index.html'),'utf8').matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)];
    s.forEach(m=>new vm.Script(m[1]));return {miniProgramScripts:files,wxmlBindings:bindings,webScripts:s.length};
  });
  fs.writeFileSync(path.join(out,'review_frontend_latency_results.json'),JSON.stringify({cases:rows},null,2));
  const failed=rows.filter(r=>!r.passed).length;
  console.log(`REVIEW_FRONTEND_LATENCY_SUMMARY total=${rows.length} passed=${rows.length-failed} failed=${failed}`);
  process.exitCode=failed?1:0;
})();
