/* Targeted failure/recovery regressions after the latency review; entirely offline. */
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const root=path.resolve(process.env.REVIEW_ROOT||path.resolve(__dirname,'../..'));
const out=path.resolve(process.env.REVIEW_OUTPUT||path.join(root,'audit/sequential_20261001/frontend'));
fs.mkdirSync(out,{recursive:true});
const rows=[],tick=()=>new Promise(r=>setImmediate(r)),silent={log(){},warn(){},error(){}};
function app(){return {globalData:{apiBase:'https://server.invalid',historyList:[],mediaCache:{},lightPoints:200},setBalance(n){this.globalData.lightPoints=n;},persist(){}};}
function apiModule(wx,a=app()){
 const module={exports:{}};
 vm.runInNewContext(fs.readFileSync(path.join(root,'miniprogram/utils/api.js'),'utf8'),{module,wx,getApp:()=>a,console:silent,setTimeout,Date});
 return module.exports;
}
function page(name,api,a=app(),extraWx={},clock={}){
 let p;const wx={showToast(){},showModal(){},showLoading(){},hideLoading(){},navigateTo(){},stopPullDownRefresh(){},...extraWx};
 vm.runInNewContext(fs.readFileSync(path.join(root,`miniprogram/pages/${name}/${name}.js`),'utf8'),
  {Page:x=>p=x,require:()=>api,getApp:()=>a,wx,console:silent,Date:clock.Date||Date,
   setTimeout:clock.setTimeout||setTimeout,clearTimeout:clock.clearTimeout||clearTimeout});
 p.data=JSON.parse(JSON.stringify(p.data));p.setData=d=>Object.assign(p.data,d);return p;
}
function timerClock(){let id=0;const timers=new Map();return {timers,setTimeout(fn,ms){timers.set(++id,{fn,ms});return id;},clearTimeout(id){timers.delete(id);}};}
async function test(name,fn){
 if(process.env.REVIEW_CASE_PATTERN&&!new RegExp(process.env.REVIEW_CASE_PATTERN).test(name))return;
 try{const observed=await fn();rows.push({case:name,passed:true,observed});console.log('PASS '+name+': '+JSON.stringify(observed||{}));}
 catch(e){rows.push({case:name,passed:false,error:String(e.stack)});console.log('FAIL '+name+': '+e.message);}
}
(async()=>{
 await test('text_login_failure_before_post_allows_retry_after_network_recovers',async()=>{
  let token='',offline=true,posts=0,logins=0;const a=app();
  const api=apiModule({getStorageSync:()=>token,setStorageSync:(k,v)=>token=v,
   login:o=>{logins++;offline?o.fail({errMsg:'network timeout'}):o.success({code:'fixture'});},
   request:o=>{if(o.url.endsWith('/api/auth/login'))o.success({statusCode:200,data:{token:'session'}});
    else if(o.url.endsWith('/api/text-generation')){posts++;o.success({statusCode:200,data:{code:0,job_id:'recovered'}});}
    else o.success({statusCode:200,data:{}});}},a);
  const p=page('text-generation',api,a);p.data.ready=true;p.data.prompt='森林小屋';
  await p.onGenerate();const before={posts,uncertain:!!p._uncertain};offline=false;await p.onGenerate();
  assert.equal(before.posts,0);assert.equal(before.uncertain,false);assert.equal(posts,1);
  return {before,afterPosts:posts,logins};
 });
 await test('text_final_post_network_loss_still_blocks_repeat_charge',async()=>{
  let posts=0;const api=apiModule({getStorageSync:()=> 'session',request:o=>{posts++;o.fail({errMsg:'request:fail timeout'});}});
  const p=page('text-generation',api);p.data.ready=true;p.data.prompt='森林小屋';await p.onGenerate();await p.onGenerate();
  assert.equal(posts,1);assert(p._uncertain);return {posts,uncertain:!!p._uncertain};
 });
 await test('successful_login_remains_usable_when_storage_write_fails',async()=>{
  let logins=0,headers=[];const api=apiModule({getStorageSync:()=> '',setStorageSync(){throw new Error('storage full');},
   login:o=>{logins++;o.success({code:'fixture'});},request:o=>{
    if(o.url.endsWith('/api/auth/login'))o.success({statusCode:200,data:{token:'memory-session'}});
    else {headers.push(o.header.Authorization||'');o.success(o.header.Authorization==='Bearer memory-session'?
     {statusCode:200,data:{balance:100}}:{statusCode:401,data:{detail:'login required'}});}}});
  let result,error;try{result=await api.me();}catch(e){error=e;}
  assert(!error,'login succeeded but protected request failed: '+(error&&error.code)+'; headers='+JSON.stringify(headers));
  assert.equal(result.balance,100);assert.equal(logins,1);return {logins,headers};
 });
 await test('forced_login_prefers_fresh_token_when_storage_keeps_expired_token',async()=>{
  let logins=0,headers=[];const api=apiModule({getStorageSync:()=> 'expired-session',setStorageSync(){throw new Error('storage full');},
   login:o=>{logins++;o.success({code:'fixture'});},request:o=>{
    if(o.url.endsWith('/api/auth/login'))o.success({statusCode:200,data:{token:'fresh-session'}});
    else {headers.push(o.header.Authorization||'');o.success(o.header.Authorization==='Bearer fresh-session'?
     {statusCode:200,data:{balance:100}}:{statusCode:401,data:{detail:'expired'}});}}});
  assert.equal(await api.ensureLogin(true),'fresh-session');let result,error;try{result=await api.me();}catch(e){error=e;}
  assert(!error,'forced login reused stale storage token: '+JSON.stringify(headers));
  assert.equal(result.balance,100);assert.equal(logins,1);assert.equal(headers.join(','),'Bearer fresh-session');
  return {logins,headers};
 });
 await test('concurrent_401_refresh_shares_new_memory_token_when_persistence_fails',async()=>{
  let logins=0,loginCallback;const headers=[];const api=apiModule({getStorageSync:()=> 'expired-session',
   setStorageSync(){throw new Error('storage full');},login:o=>{logins++;loginCallback=o;},request:o=>{
    if(o.url.endsWith('/api/auth/login'))o.success({statusCode:200,data:{token:'fresh-session'}});
    else {headers.push(o.header.Authorization||'');o.success(o.header.Authorization==='Bearer fresh-session'?
     {statusCode:200,data:{balance:100}}:{statusCode:401,data:{detail:'expired'}});}}});
  const one=api.me(),two=api.me();await tick();loginCallback.success({code:'fixture'});
  const result=await Promise.all([one,two]);assert(result.every(r=>r.balance===100));assert.equal(logins,1);
  assert.equal(headers.filter(h=>h==='Bearer fresh-session').length,2);return {logins,headers};
 });
 await test('clearing_persisted_token_does_not_resurrect_previous_memory_login',async()=>{
  let token='',logins=0;const headers=[];const api=apiModule({getStorageSync:()=>token,setStorageSync:(k,v)=>token=v,
   login:o=>{logins++;o.success({code:'fixture'});},request:o=>{
    if(o.url.endsWith('/api/auth/login'))o.success({statusCode:200,data:{token:'session-'+logins}});
    else {headers.push(o.header.Authorization);o.success({statusCode:200,data:{balance:100}});}}});
  await api.me();token='';await api.me();assert.equal(logins,2);
  assert.equal(headers.join(','),'Bearer session-1,Bearer session-2');return {logins,headers};
 });
 await test('external_token_replacement_supersedes_previous_memory_login',async()=>{
  let token='',logins=0;const headers=[];const api=apiModule({getStorageSync:()=>token,setStorageSync:(k,v)=>token=v,
   login:o=>{logins++;o.success({code:'fixture'});},request:o=>{
    if(o.url.endsWith('/api/auth/login'))o.success({statusCode:200,data:{token:'first-session'}});
    else {headers.push(o.header.Authorization);o.success({statusCode:200,data:{balance:100}});}}});
  await api.me();token='other-session';await api.me();assert.equal(logins,1);
  assert.equal(headers.join(','),'Bearer first-session,Bearer other-session');return {logins,headers};
 });
 await test('failed_shared_login_is_cleared_and_next_attempt_can_succeed',async()=>{
  let token='',loginCallback,calls=0;const api=apiModule({getStorageSync:()=>token,setStorageSync:(k,v)=>token=v,
   login:o=>{calls++;loginCallback=o;},request:o=>o.success({statusCode:200,data:{token:'session'}})});
  const p1=api.ensureLogin(),p2=api.ensureLogin(),p3=api.ensureLogin();loginCallback.fail({errMsg:'offline'});
  const failed=await Promise.allSettled([p1,p2,p3]);const retry=api.ensureLogin();loginCallback.success({code:'fixture'});
  assert.equal(await retry,'session');assert(failed.every(r=>r.status==='rejected'));assert.equal(calls,2);
  return {failedWaiters:failed.length,loginAttempts:calls};
 });
 await test('failed_shared_config_is_cleared_and_next_attempt_fetches_fresh',async()=>{
  let callback,calls=0;const api=apiModule({getStorageSync:()=> '',request:o=>{calls++;callback=o;}});
  const p1=api.config(),p2=api.config();callback.fail({errMsg:'offline'});await Promise.allSettled([p1,p2]);
  const retry=api.config();callback.success({statusCode:200,data:{prices:{light:80}}});
  assert.equal((await retry).prices.light,80);assert.equal(calls,2);return {configAttempts:calls};
 });
 await test('late_list_snapshot_preserves_newly_acknowledged_background_job',async()=>{
  let reply;const a=app(),clock=timerClock();const p=page('works',{myJobs:()=>new Promise(r=>reply=r),absolute:x=>x},a,{},clock);
  const loading=p.onShow();await tick();
  // The server captured an empty list before an upload accepted on a hidden adjust page.
  a.globalData.historyList.unshift({jobId:'new-accepted',status:'processing',createdAt:Date.now()/1000});a.persist();
  reply({jobs:[]});await loading;
  assert(a.globalData.historyList.some(w=>w.jobId==='new-accepted'),'older list snapshot deleted a newly accepted job');
  assert.equal(clock.timers.size,1);p.onHide();return {acceptedJobs:a.globalData.historyList.length};
 });
 await test('older_missing_jobs_are_not_revived_when_preserving_new_background_acceptance',async()=>{
  let reply;const a=app();a.globalData.historyList=[{jobId:'deleted',status:'processing'},{jobId:'expired',status:'succeeded',result:'old'}];
  const p=page('works',{myJobs:()=>new Promise(r=>reply=r),absolute:x=>x},a);
  const loading=p.loadWorks();a.globalData.historyList.unshift({jobId:'just-accepted',status:'processing'});
  reply({jobs:[]});await loading;assert.equal(a.globalData.historyList.map(w=>w.jobId).join(','),'just-accepted');
  const next=p.loadWorks(true);reply({jobs:[]});await next;assert.equal(a.globalData.historyList.length,0);
  return {firstSnapshotKept:'just-accepted',nextAuthoritativeSnapshotRemovedMissingJob:true};
 });
 await test('manual_list_retry_starts_auto_refresh_after_initial_failure',async()=>{
  let calls=0;const clock=timerClock(),p=page('works',{myJobs:async()=>{if(++calls===1)throw new Error('offline');
   return {jobs:[{id:'accepted',status:'processing'}]};},absolute:x=>x},app(),{},clock);
  await p.onShow();assert.equal(clock.timers.size,0);await p.onRetryWorks();const scheduled=clock.timers.size;
  p.onHide();assert.equal(scheduled,1);return {listRequests:calls,scheduledAfterRetry:scheduled};
 });
 await test('newer_list_response_wins_over_concurrent_older_request',async()=>{
  const replies=[],a=app(),p=page('works',{myJobs:()=>new Promise(r=>replies.push(r)),absolute:x=>x},a);
  const older=p.loadWorks(true),newer=p.loadWorks(true);
  replies[1]({jobs:[{id:'latest',status:'failed',error:'latest failure'}]});await newer;
  replies[0]({jobs:[{id:'older',status:'processing'}]});await older;
  assert.equal(a.globalData.historyList[0].jobId,'latest');assert.equal(p.data.loading,false);
  return {displayedJob:a.globalData.historyList[0].jobId};
 });
 fs.writeFileSync(path.join(out,'sequential_frontend_results.json'),JSON.stringify({cases:rows},null,2));
 const failed=rows.filter(r=>!r.passed).length;
 console.log(`SEQUENTIAL_FRONTEND_SUMMARY total=${rows.length} passed=${rows.length-failed} failed=${failed}`);process.exitCode=failed?1:0;
})();
