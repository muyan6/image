/* Community user journeys with wx/HTTP doubles; no real posting or credits. */
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const root=path.resolve(process.env.REVIEW_ROOT||path.resolve(__dirname,'../..'));
const out=path.resolve(process.env.REVIEW_OUTPUT||path.join(root,'audit/community-ui'));
const tick=()=>new Promise(r=>setImmediate(r)),rows=[];
function page(name,api={},wx={}){
 let p;const messages=[],app={globalData:{historyList:[],mediaCache:{}},persist(){}};
 vm.runInNewContext(fs.readFileSync(path.join(root,`miniprogram/pages/${name}/${name}.js`),'utf8'),{
  Page:x=>p=x,require:()=>api,getApp:()=>app,console,setTimeout,clearTimeout,
  wx:{showToast:o=>messages.push(o),showModal:o=>messages.push(o),navigateTo:o=>messages.push(o),switchTab:o=>messages.push(o),...wx}});
 p.data=JSON.parse(JSON.stringify(p.data));p.setData=d=>Object.assign(p.data,d);return {p,messages};
}
const job={id:'abc123abc123',status:'succeeded',result_url:'https://cos.invalid/result',orig_url:'https://cos.invalid/comparison',template_name:'胶片'};
async function test(name,fn){try{await fn();rows.push({case:name,passed:true});}catch(e){console.error(name,e);rows.push({case:name,passed:false,error:e.stack});}}
(async()=>{
 await test('submission_route_registered_and_visible_on_work_cards',()=>{
  assert(JSON.parse(fs.readFileSync(path.join(root,'miniprogram/app.json'))).pages.includes('pages/community-submit/community-submit'));
  const xml=fs.readFileSync(path.join(root,'miniprogram/pages/works/works.wxml'),'utf8');assert(xml.includes('投稿到社区'));assert(xml.includes('catchtap="onSubmitWork"'));
  const t=page('works');t.p.data.works=[{jobId:job.id,status:'succeeded'}];t.p.onSubmitWork({currentTarget:{dataset:{index:0}}});assert(t.messages[0].url.endsWith('?job='+job.id));
 });
 await test('unfinished_work_has_no_submission_navigation',()=>{
  const t=page('works');t.p.openSubmission({jobId:job.id,status:'processing'});assert(!t.messages.some(x=>x.url));
 });
 await test('form_defaults_to_result_only_and_requires_separate_consent',async()=>{
  let calls=0;const t=page('community-submit',{absolute:x=>x,request:async()=>{calls++;return job;}});
  await t.p.loadJob(job.id);assert(!t.p.data.shareOriginal);assert(!t.p.data.consent);await t.p.onSubmit();assert.equal(calls,1);
 });
 await test('changing_original_visibility_invalidates_old_consent',()=>{
  const t=page('community-submit');t.p.data.consent=true;t.p.onOriginal({detail:{value:true}});assert(t.p.data.shareOriginal);assert(!t.p.data.consent);
 });
 await test('submit_sends_job_and_consent_not_urls_author_or_reward',async()=>{
  let sent;const t=page('community-submit',{absolute:x=>x,request:async(url,opts)=>{
   if(url==='/api/community/submissions'){sent=opts.data;return {submission:{status:'pending'}};}
   if(url.includes('/mine'))return {items:[],has_more:false};return job;
  }});await t.p.loadJob(job.id);t.p.data.consent=true;await t.p.onSubmit();
  assert.equal(sent.job_id,job.id);assert.strictEqual(sent.consent,true);assert.strictEqual(sent.share_original,false);
  assert(!('result_url' in sent));assert(!('author_name' in sent));assert(!('reward' in sent));assert(!t.p.data.consent);
 });
 await test('double_tap_keeps_one_submission_inflight',async()=>{
  let resolve,count=0;const t=page('community-submit',{request:async(url)=>{if(url.includes('/mine'))return {items:[]};count++;return new Promise(r=>resolve=r);}});
  Object.assign(t.p.data,{job,jobId:job.id,title:'作品',consent:true});const a=t.p.onSubmit();await t.p.onSubmit();assert.equal(count,1);resolve({submission:{status:'pending'}});await a;
 });
 await test('failed_submission_keeps_draft_and_explains_server_reconciliation',async()=>{
  const t=page('community-submit',{request:async()=>{throw new Error('offline');}});Object.assign(t.p.data,{job,jobId:job.id,title:'我的草稿',consent:true});await t.p.onSubmit();
  assert.equal(t.p.data.title,'我的草稿');assert(t.p.data.submitError.includes('同一作品'));assert(!t.p.data.submitting);
 });
 await test('my_submissions_pages_append_without_duplicates',async()=>{
  const t=page('community-submit',{request:async url=>url.endsWith('offset=0')?{items:[{id:'a',status:'pending'}],next_offset:1,has_more:true}:{items:[{id:'b',status:'published'}],next_offset:2,has_more:false}});
  await t.p.loadMine();await t.p.onLoadMore();assert.equal(t.p.data.items.length,2);assert.equal(t.p.data.items[0].statusLabel,'等待审核');
 });
 await test('withdrawal_freezes_id_and_revision_at_confirmation',async()=>{
  let modal,sent;const t=page('community-submit',{request:async(url,opts)=>{if(opts)sent={url,...opts};return {items:[]};}},{showModal:o=>modal=o.success});
  t.p.data.items=[{id:'s_one',revision:2}];t.p.onWithdraw({currentTarget:{dataset:{id:'s_one'}}});t.p.data.items=[{id:'s_two',revision:3}];await modal({confirm:true});
  assert(sent.url.endsWith('/s_one/withdraw'));assert.equal(sent.data.revision,2);
 });
 await test('stale_job_response_does_not_replace_new_selection',async()=>{
  let first;const t=page('community-submit',{absolute:x=>x,request:url=>url.endsWith('/first')?new Promise(r=>first=r):Promise.resolve({...job,id:'second',template_name:'第二件'})});
  const pending=t.p.loadJob('first');await t.p.loadJob('second');first(job);await pending;assert.equal(t.p.data.job.id,'second');
 });
 await test('likes_use_server_response_and_do_not_increment_on_failure',async()=>{
  let request;const t=page('community',{request:async(url,options)=>{request={url,options};return {likes:9,liked:true};}});
  t.p.data.items=[{id:'s_one',likes:2,liked:false}];await t.p.onLikeItem({currentTarget:{dataset:{id:'s_one'}}});
  assert.equal(request.options.method,'PUT');assert.strictEqual(request.options.data.liked,true);assert.equal(t.p.data.items[0].likes,9);
  const bad=page('community',{request:async()=>{throw new Error('offline');}});bad.p.data.items=[{id:'s_one',likes:2,liked:false}];await bad.p.onLikeItem({currentTarget:{dataset:{id:'s_one'}}});assert.equal(bad.p.data.items[0].likes,2);
 });
 await test('like_double_tap_does_not_send_two_mutations',async()=>{
  let done,count=0;const t=page('community',{request:()=>{count++;return new Promise(r=>done=r);}});t.p.data.items=[{id:'s_one',likes:0,liked:false}];
  const p=t.p.onLikeItem({currentTarget:{dataset:{id:'s_one'}}});await t.p.onLikeItem({currentTarget:{dataset:{id:'s_one'}}});assert.equal(count,1);done({likes:1,liked:true});await p;
 });
 await test('community_errors_do_not_pretend_closed_or_delete_cached_posts',async()=>{
  const t=page('community',{request:async()=>{throw new Error('offline');}});t.p.data.items=[{id:'cached'}];t.p.data.enabled=true;await t.p.loadCommunity(true);
  assert.equal(t.p.data.items.length,1);assert(t.p.data.enabled);assert(t.p.data.loadError);
 });
 await test('result_only_posts_do_not_render_empty_original_frame',()=>{
  const xml=fs.readFileSync(path.join(root,'miniprogram/pages/community/community.wxml'),'utf8');assert(xml.includes('wx:if="{{item.origUrl}}"'));assert(xml.includes('compare-frame-single'));assert(xml.includes('每件作品仅一次'));
 });
 await test('private_original_preview_matches_authorized_comparison',()=>{
  const xml=fs.readFileSync(path.join(root,'miniprogram/pages/community-submit/community-submit.wxml'),'utf8');assert(xml.includes('shareOriginal && job.orig_url'));assert(xml.includes('仅成品，不公开原图'));assert(xml.includes('disabled="{{!consent || submitting}}"'));
 });
 await test('social_mutations_use_authenticated_api_adapter',async()=>{
  let authorization;const module={exports:{}};
  vm.runInNewContext(fs.readFileSync(path.join(root,'miniprogram/utils/api.js'),'utf8'),{module,getApp:()=>({globalData:{apiBase:'https://fixture.invalid'}}),setTimeout,console,
   wx:{getStorageSync:()=> 'fixture-token',request:o=>{authorization=o.header.Authorization;o.success({statusCode:200,data:{liked:true,likes:1}});}}});
  await module.exports.request('/api/community/posts/s_fixture/like',{method:'PUT',data:{liked:true}});assert.equal(authorization,'Bearer fixture-token');
 });
 await test('admin_queue_and_review_actions_keep_legacy_editor',()=>{
  const html=fs.readFileSync(path.join(root,'backend/admin.html'),'utf8');assert(html.includes('用户投稿审核'));assert(html.includes('reviewSubmission'));assert(html.includes('revision:p.revision'));assert(html.includes('openCommunityEditor'));assert(html.includes('不重复发奖'));
 });
 await test('all_new_wxml_events_exist_and_scripts_parse',()=>{
  for(const name of ['community-submit','community','works']){
   const js=fs.readFileSync(path.join(root,`miniprogram/pages/${name}/${name}.js`),'utf8');new vm.Script(js);
   const xml=fs.readFileSync(path.join(root,`miniprogram/pages/${name}/${name}.wxml`),'utf8');
   const p=page(name).p;for(const match of xml.matchAll(/(?:bind\w+|catch\w+)="([A-Za-z_]\w*)"/g)){if(match[1]!=='true')assert.equal(typeof p[match[1]],'function',match[1]);}
  }
 });
 fs.mkdirSync(out,{recursive:true});fs.writeFileSync(path.join(out,'community_submission_frontend_results.json'),JSON.stringify({cases:rows},null,2));
 const failed=rows.filter(r=>!r.passed).length;console.log(`COMMUNITY_SUBMISSION_FRONTEND_SUMMARY total=${rows.length} passed=${rows.length-failed} failed=${failed}`);process.exitCode=failed?1:0;
})();
