/* Offline regressions for full-review frontend races and paginated profile counts. */
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const root=path.resolve(process.env.REVIEW_ROOT||path.resolve(__dirname,'../..'));
const out=path.resolve(process.env.REVIEW_OUTPUT||path.join(root,'audit/full-review-frontend'));
fs.mkdirSync(out,{recursive:true});
const rows=[],quiet={log(){},warn(){},error(){}},tick=()=>new Promise(r=>setImmediate(r));
function app(history=[]){return {globalData:{historyList:history,mediaCache:{},lightPoints:200},setBalance(n){this.globalData.lightPoints=n;},persist(){}};}
function page(name,api,a=app(),extraWx={}){
 let p;const wx={showToast(){},showModal(){},showLoading(){},hideLoading(){},hideKeyboard(){},navigateTo(){},redirectTo(){},vibrateShort(){},...extraWx};
 vm.runInNewContext(fs.readFileSync(path.join(root,'miniprogram/pages',name,name+'.js'),'utf8'),{getApp:()=>a,require:()=>api,Page:x=>p=x,wx,console:quiet,setTimeout,clearTimeout,Date});
 p.data=JSON.parse(JSON.stringify(p.data));p.setData=x=>Object.assign(p.data,x);return p;
}
async function test(name,fn){
 try{const observed=await fn();rows.push({case:name,passed:true,observed});console.log('PASS '+name+': '+JSON.stringify(observed||{}));}
 catch(e){rows.push({case:name,passed:false,error:String(e.stack)});console.log('FAIL '+name+': '+e.message);}
}
function qrFixture(){
 const els={},el=id=>els[id]||={style:{},classList:{add(){},remove(){}},files:[],click(){}};
 const intervals=new Map(),cleared=[];let sequence=0,starts=0,finishOldStatus;
 const response=(d,status=200)=>({ok:status>=200&&status<300,status,json:async()=>d,blob:async()=>({})});
 const sandbox={document:{getElementById:el,createElement:()=>el('login-panel'),body:{appendChild(){}}},window:{},console:quiet,
  localStorage:{getItem:()=>'',setItem(){},removeItem(){}},URL:{createObjectURL:()=> 'blob:fixture',revokeObjectURL(){}},
  setInterval:f=>{const id=++sequence;intervals.set(id,f);return id;},clearInterval:id=>{cleared.push(id);intervals.delete(id);},setTimeout,Date,alert(){},
  fetch:async url=>{if(url==='/api/auth/web')return response({detail:'login needed'},401);
   if(url==='/api/auth/wechat-web/start')return response({id:'qr_'+(++starts),qr_url:'/qr_'+starts});
   if(url==='/qr_1'||url==='/qr_2')return response({});
   if(url==='/api/auth/wechat-web/qr_1/status')return new Promise(r=>finishOldStatus=r);
   return response({state:'pending'});}};
 vm.createContext(sandbox);const script=[...fs.readFileSync(path.join(root,'backend/index.html'),'utf8').matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)][0][1];vm.runInContext(script,sandbox);
 return {els,el,intervals,cleared,response,run:s=>vm.runInContext(s,sandbox),completeOld:(data,status)=>finishOldStatus(response(data,status))};
}
(async()=>{
 await test('adjust_return_during_accepted_upload_resumes_current_job',async()=>{
  let resolveSubmit,submits=0,polls=0,redirects=0;const a=app();
  const p=page('adjust',{submitJob:()=>{submits++;return new Promise(r=>resolveSubmit=r);},absolute:x=>x,
   waitForJob:async()=>{polls++;return {status:'succeeded',orig_url:'https://cos.invalid/orig',result_url:'https://cos.invalid/result'};}},a,{redirectTo:()=>redirects++});
  p._foreground=true;p.data.priceReady=true;p.data.lightPoints=200;
  const pending=p.executeUpload('fixture.jpg');p.onHide();p.onShow();await p.onStartGenerate();
  assert.strictEqual(submits,1);resolveSubmit({code:0,job_id:'accepted_job',balance:160,orig_url:'https://cos.invalid/orig'});await pending;
  assert.strictEqual(p.data.processing,false);assert.strictEqual(p.data.currentJobId,'accepted_job');assert.strictEqual(p._submissionPending,false);
  assert.strictEqual(polls,1);assert.strictEqual(redirects,1);assert.strictEqual(a.globalData.historyList[0].status,'succeeded');
  return {submits,polls,redirects,mask:p.data.processing,currentJobId:p.data.currentJobId};
 });
 await test('adjust_return_during_rejected_upload_releases_mask_and_reports_failure',async()=>{
  let rejectSubmit,modal;const p=page('adjust',{submitJob:()=>new Promise((r,j)=>rejectSubmit=j)},app(),{showModal:o=>modal=o});
  p._foreground=true;const pending=p.executeUpload('fixture.jpg');p.onHide();p.onShow();rejectSubmit(Object.assign(new Error('insufficient balance'),{status:402}));await pending;
  assert.strictEqual(p.data.processing,false);assert.strictEqual(p._submissionPending,false);assert.strictEqual(modal.title,'光子余额不足');
  return {mask:p.data.processing,pending:p._submissionPending,modal:modal.title};
 });
 await test('adjust_hidden_submission_keeps_job_without_foreground_updates',async()=>{
  let resolveSubmit,polls=0;const a=app();const p=page('adjust',{submitJob:()=>new Promise(r=>resolveSubmit=r),absolute:x=>x,
   waitForJob:async()=>{polls++;}},a);
  p._foreground=true;const pending=p.executeUpload('fixture.jpg');p.onHide();resolveSubmit({code:0,job_id:'accepted_hidden'});await pending;
  assert.strictEqual(p.data.processing,false);assert.strictEqual(polls,0);assert.strictEqual(a.globalData.historyList[0].jobId,'accepted_hidden');
  return {polls,mask:p.data.processing,savedJobs:a.globalData.historyList.length};
 });
 await test('old_poll_completion_preserves_new_upload_lock_and_ui',async()=>{
  let finishPoll,finishSecond,submits=0;const p=page('adjust',{submitJob:()=>++submits===1?Promise.resolve({code:0,job_id:'first'}):new Promise(r=>finishSecond=r),
   absolute:x=>x,waitForJob:()=>new Promise(r=>finishPoll=r)});
  p._foreground=true;const first=p.executeUpload('fixture.jpg');await tick();p.onHide();p.onShow();
  const second=p.executeUpload('fixture.jpg');finishPoll({status:'succeeded'});await first;
  assert(p._submissionPending);assert(p.data.processing);p.onHide();finishSecond({code:0,job_id:'second'});await second;
  return {submits,newUploadStayedLocked:true};
 });
 await test('old_background_poll_failure_preserves_new_upload_mask',async()=>{
  let rejectPoll,finishSecond,submits=0;const p=page('adjust',{submitJob:()=>++submits===1?Promise.resolve({code:0,job_id:'first'}):new Promise(r=>finishSecond=r),
   absolute:x=>x,waitForJob:()=>new Promise((r,j)=>rejectPoll=j)});
  p._foreground=true;const first=p.executeUpload('fixture.jpg');await tick();p.onHide();p.onShow();
  const second=p.executeUpload('fixture.jpg');rejectPoll(Object.assign(new Error('background'),{code:'USER_BACKGROUND'}));await first;
  const mask=p.data.processing,locked=p._submissionPending;p.onHide();finishSecond({code:0,job_id:'second'});await second;
  assert(locked);assert(mask);return {submits,newUploadStayedLocked:locked,newMaskStayedVisible:mask};
 });
 for(const state of ['expired','denied','approved'])await test('stale_qr_'+state+'_does_not_change_new_login',async()=>{
  const q=qrFixture();await tick();const oldTimer=[...q.intervals.keys()][0];const oldPolling=q.intervals.get(oldTimer)();await tick();
  await q.run('openWechatLogin(true)');const newTimer=[...q.intervals.keys()][0];assert(newTimer&&newTimer!==oldTimer);
  q.completeOld(state==='expired'?{detail:'old QR expired'}:state==='denied'?{state:'denied'}:{state:'approved',token:'old-account-token',balance:999},state==='expired'?410:200);await oldPolling;
  assert(q.intervals.has(newTimer));assert.strictEqual(q.run('webToken'),'');assert.strictEqual(q.run('loginPanel.style.display'),'flex');
  assert.strictEqual(q.el('wechatLoginMessage').textContent,'请用微信扫一扫，登录码 5 分钟内有效。');
  return {oldTimer,newTimer,activePolling:q.intervals.size,token:q.run('webToken')};
 });
 await test('comment_publish_queues_refresh_after_older_snapshot',async()=>{
  let finishOldList,reads=0,posts=0;const p=page('community-detail',{request:async(url,o)=>{if(o?.method==='POST'){posts++;return {ok:true};}
   reads++;return reads===1?new Promise(r=>finishOldList=r):{items:[{id:'new_comment',created_at:1,content:'new comment'}],total:1,next_offset:1};}});
  p._id='post_fixture';p.data.post={id:p._id};p.data.draft='new comment';const initial=p.loadComments(),posting=p.onSend();await tick();
  finishOldList({items:[],total:0,next_offset:0});await Promise.all([initial,posting]);
  assert.strictEqual(posts,1);assert.strictEqual(reads,2);assert.strictEqual(p.data.comments[0].id,'new_comment');assert.strictEqual(p.data.commentTotal,1);
  return {posts,reads,comments:p.data.comments.length,total:p.data.commentTotal};
 });
 await test('comment_delete_during_load_more_refreshes_from_first_page',async()=>{
  let finishOldList,reads=0,deleted=0;const offsets=[];let finishDelete;
  const p=page('community-detail',{request:async(url,o)=>{if(o?.method==='DELETE'){deleted++;return {ok:true};}
   reads++;offsets.push(/offset=(\d+)/.exec(url)?.[1]);return reads===1?new Promise(r=>finishOldList=r):{items:[],total:0,next_offset:0};}},app(),{showModal:o=>finishDelete=o.success({confirm:true})});
  p._id='post_fixture';p.data.post={id:p._id};p.data.hasMore=true;p._offset=30;p.data.comments=[{id:'deleted_comment'}];
  const initial=p.loadComments(true);p.onDeleteComment({currentTarget:{dataset:{id:'deleted_comment'}}});await tick();
  finishOldList({items:[{id:'stale_comment',created_at:1}],total:2,next_offset:31});await Promise.all([initial,finishDelete]);
  assert.strictEqual(deleted,1);assert.deepStrictEqual(offsets,['30','0']);assert.strictEqual(p.data.comments.length,0);assert.strictEqual(p.data.commentTotal,0);
  return {deleted,reads,offsets,total:p.data.commentTotal};
 });
 await test('profile_uses_summary_without_truncating_full_history_cache',async()=>{
  const history=Array.from({length:101},(_,i)=>({jobId:'job_'+i,status:'succeeded',createdAt:200-i})),a=app(history),calls=[];
  const p=page('my',{config:async()=>({prices:{light:40,fine:40},ads:{}}),me:async()=>({balance:200,earn:{}}),absolute:x=>x,
   myJobs:async(limit,offset)=>{calls.push({limit,offset:offset||0});return {jobs:history.slice(0,100).map(x=>({id:x.jobId,status:x.status,created_at:x.createdAt})),has_more:true,next_offset:100,total:151,processing_count:4};}},a);
  p.refreshUserData();await tick();assert.strictEqual(p.data.worksTotal,151);assert.strictEqual(p.data.processingCount,4);assert.strictEqual(a.globalData.historyList.length,101);
  assert.strictEqual(calls.length,1);assert.strictEqual(calls[0].limit,100);
  return {worksTotal:p.data.worksTotal,processing:p.data.processingCount,cachedJobs:a.globalData.historyList.length,requests:calls.length};
 });
 await test('profile_complete_cloud_page_removes_deleted_cached_jobs',async()=>{
  const a=app([{jobId:'deleted',status:'succeeded'}]);const p=page('my',{config:async()=>({ads:{}}),me:async()=>({earn:{}}),absolute:x=>x,
   myJobs:async()=>({jobs:[],has_more:false,total:0,processing_count:0})},a);
  p.refreshUserData();await tick();assert.strictEqual(a.globalData.historyList.length,0);assert.strictEqual(p.data.worksTotal,0);assert.strictEqual(p.data.processingCount,0);
  return {worksTotal:p.data.worksTotal,cachedJobs:a.globalData.historyList.length};
 });
 await test('profile_summary_survives_throttled_reentry_and_pending_refresh',async()=>{
  let calls=0,finishRefresh;const history=Array.from({length:100},(_,i)=>({jobId:'job_'+i,status:'succeeded',createdAt:200-i}));
  const response={jobs:history.map(w=>({id:w.jobId,status:w.status,created_at:w.createdAt})),has_more:true,total:151,processing_count:4};
  const p=page('my',{config:async()=>({ads:{}}),me:async()=>({earn:{}}),absolute:x=>x,
   myJobs:async()=>++calls===1?response:new Promise(r=>finishRefresh=r)},app(history));
  p.onShow();await tick();p.onShow();await tick();
  assert.strictEqual(calls,1);assert.strictEqual(p.data.worksTotal,151);assert.strictEqual(p.data.processingCount,4);
  p._lastProfileSync=Date.now()-31000;p.onShow();await tick();
  assert.strictEqual(calls,2);assert.strictEqual(p.data.worksTotal,151);assert.strictEqual(p.data.processingCount,4);
  finishRefresh(response);await tick();return {calls,countWhileThrottled:151,countWhileRefreshPending:p.data.worksTotal,processing:4};
 });
 await test('profile_all_works_labels_use_authoritative_count',()=>{
  const xml=fs.readFileSync(path.join(root,'miniprogram/pages/my/my.wxml'),'utf8');assert(xml.includes('全部作品 ({{ worksTotal }})'));
  assert(/class="stat-number">\{\{ worksTotal \}\}/.test(xml));return {countBinding:'worksTotal'};
 });
 fs.writeFileSync(path.join(out,'full_review_frontend_results.json'),JSON.stringify({source:root,cases:rows},null,2),'utf8');
 const failed=rows.filter(x=>!x.passed).length;console.log(`FULL_REVIEW_FRONTEND_SUMMARY total=${rows.length} passed=${rows.length-failed} failed=${failed}`);process.exitCode=failed?1:0;
})();
