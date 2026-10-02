/* Offline experience checks: only mocked user interfaces, images and APIs. */
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const root=path.resolve(process.env.REVIEW_ROOT||path.resolve(__dirname,'../..'));
const out=path.resolve(process.env.REVIEW_OUTPUT||path.join(root,'audit/experience-retention-tests'));
const cases=[],tick=()=>new Promise(r=>setImmediate(r));
const now=1760000000000;class Clock extends Date{static now(){return now;}}
const counts={all:60,processing:2,succeeded:57,failed:1};
const job=(id,status='succeeded',extra={})=>({id,status,created_at:now/1000-1000,result_url:status==='succeeded'?'https://cos.invalid/'+id+'.jpg':'',orig_url:'',...extra});
function fixture(name,overrides={},wxOverrides={},prefOverrides={},creationOverrides={}){
 let p;const calls=[],messages=[],app={globalData:{historyList:[],mediaCache:{},lightPoints:0,freeMode:false},persist(){},setBalance(n){this.globalData.lightPoints=n;},setNickname(n){this.globalData.nickname=n;},clearHistory(){this.globalData.historyList=[];}};
 const api={absolute:x=>x,config:async()=>({}),me:async()=>({balance:0,earn:{}}),myJobs:async()=>({jobs:[],total:0,status_counts:{all:0,processing:0,succeeded:0,failed:0}}),
  downloadJobMedia:async()=> 'wxfile://tmp/result.jpg',templates:async()=>({items:[],groups:[]}),
  request:async(url,options)=>{calls.push({url,options});return {items:[],total:0,has_more:false,next_offset:0};},...overrides};
 const prefs={load:async()=>({template_favorites:[],recent_templates:[]}),setFavorite:async()=>({template_favorites:[],recent_templates:[]}),...prefOverrides};
 const creation={recreateFromJob:async()=>true,...creationOverrides};
 const wx={showToast:o=>messages.push(o),showModal:o=>messages.push(o),showActionSheet:o=>messages.push(o),navigateTo:o=>messages.push(o),switchTab:o=>messages.push(o),
  pageScrollTo:o=>messages.push(o),getStorageSync(){},setStorageSync(){},showLoading(){},hideLoading(){},stopPullDownRefresh(){},openSetting(){},saveImageToPhotosAlbum:o=>o.success({}),...wxOverrides};
 vm.runInNewContext(fs.readFileSync(path.join(root,`miniprogram/pages/${name}/${name}.js`),'utf8'),{
  Page:x=>p=x,getApp:()=>app,require:n=>n.includes('/preferences')?prefs:n.includes('/creation-draft')?creation:n.includes('/commerce')?require(path.join(root,'miniprogram/utils/commerce.js')):n.includes('/payment')?{}:api,
  wx,Date:Clock,console:{warn(){},log(){}},setTimeout:()=>1,clearTimeout(){}});
 p.data=JSON.parse(JSON.stringify(p.data));p.setData=d=>Object.assign(p.data,d);return {p,api,app,calls,messages,creation,wx};
}
async function test(name,fn){try{await fn();cases.push({case:name,passed:true});console.log('PASS '+name);}catch(e){cases.push({case:name,passed:false,error:e.stack});console.error('FAIL '+name+': '+e.message);}}
const xml=name=>fs.readFileSync(path.join(root,`miniprogram/pages/${name}/${name}.wxml`),'utf8');
(async()=>{
 await test('credits_read_real_signed_records_and_paginate_without_duplicates',async()=>{
  const urls=[];const t=fixture('credits',{request:async url=>{urls.push(url);return url.includes('offset=0')?{items:[{id:'d',title:'生成扣款',amount:-40,created_at:now/1000,job_id:'j'}],total:2,has_more:true,next_offset:1}:
    {items:[{id:'d',title:'生成扣款',amount:-40,created_at:now/1000,job_id:'j'},{id:'r',title:'失败退回',amount:40,created_at:now/1000,job_id:'j'}],total:2,has_more:false,next_offset:2};}});
  await t.p.loadRecords();assert.equal(t.p.data.records[0].amountText,'-40');assert(t.p.data.recordsLoaded);await t.p.onMoreRecords();
  assert.equal(t.p.data.records.length,2);assert.equal(t.p.data.records[1].amountText,'+40');assert.equal(urls.length,2);assert(urls[1].includes('offset=1'));
 });
 await test('credits_error_is_not_empty_and_retry_preserves_then_recovers',async()=>{
  let fail=true;const t=fixture('credits',{request:async()=>{if(fail)throw Error('offline');return {items:[],total:0,has_more:false};}});
  await t.p.loadRecords();assert(t.p.data.recordsError);assert(!t.p.data.recordsLoaded);fail=false;await t.p.onRetryRecords();assert(!t.p.data.recordsError);assert(t.p.data.recordsLoaded);
  assert(xml('credits').includes('!recordsError && records.length === 0'));
 });
 await test('credits_link_records_to_their_exact_work_or_order',()=>{
  const t=fixture('credits');t.p.data.records=[{id:1,job_id:'job x'},{id:2,order_id:'order y'}];
  t.p.onOpenRecord({currentTarget:{dataset:{id:1}}});t.p.onOpenRecord({currentTarget:{dataset:{id:2}}});
  assert(t.messages[0].url.endsWith('?jobId=job%20x'));assert(t.messages[1].url.endsWith('?id=order%20y'));
 });
 await test('works_load_one_page_then_reach_bottom_merges_by_job_id',async()=>{
  const args=[];const t=fixture('works',{myJobs:async(limit,offset,status)=>{args.push({limit,offset,status});return offset===0?{jobs:[job('a')],total:60,status_counts:counts,processing_count:2,has_more:true,next_offset:1}:
    {jobs:[job('a'),job('b')],total:60,status_counts:counts,processing_count:2,has_more:false,next_offset:3};}});
  await t.p.loadWorks(true);assert.equal(args.length,1);assert.equal(args[0].limit,24);assert.equal(t.p.data.works.length,1);
  await t.p.onReachBottom();assert.equal(args.length,2);assert.equal(t.p.data.works.length,2);assert.equal(t.p.data.workFilters[0].count,60);
 });
 await test('works_status_filter_requests_server_and_uses_global_counts',async()=>{
  const args=[];const t=fixture('works',{myJobs:async(limit,offset,status)=>{args.push(status);return {jobs:[job('bad','failed',{charged_amount:40,refunded_amount:0})],total:1,status_counts:counts};}});
  await t.p.onFilterStatus({currentTarget:{dataset:{id:'failed'}}});assert.equal(args[0],'failed');assert.equal(t.p.data.total,1);assert.equal(t.p.data.workFilters[0].count,60);
  assert.equal(t.p.data.filteredWorks[0].settlementLabel,'扣除 40 · 已退回 0 光子');
 });
 await test('works_hide_discards_inflight_page_without_cache_overwrite',async()=>{
  let finish;const t=fixture('works',{myJobs:()=>new Promise(r=>finish=r)});const pending=t.p.loadWorks(true);t.p.onHide();finish({jobs:[job('late')],total:1});await pending;
  assert.equal(t.app.globalData.historyList.length,0);assert.equal(t.p.data.works.length,0);assert(!t.p.data.loading);
 });
 await test('works_deleted_jobs_do_not_reappear_from_stale_cloud_or_poll',async()=>{
  const t=fixture('works',{myJobs:async()=>({jobs:[job('deleted'),job('live')],total:2})});t.p._deletedIds=new Set(['deleted']);await t.p.loadWorks(true);
  assert.equal(t.p.data.works.length,1);assert.equal(t.p.data.works[0].jobId,'live');assert(!t.app.globalData.historyList.some(w=>w.jobId==='deleted'));
 });
 await test('works_processing_poll_does_not_reload_all_history_or_replace_loaded_cards',async()=>{
  const statuses=[],details=[];const t=fixture('works',{myJobs:async(limit,offset,status)=>{statuses.push(status);return {jobs:[job('pending','processing')],processing_count:1,status_counts:{...counts,processing:1,succeeded:58}};},
    request:async url=>{details.push(url);return job('done','succeeded',{charged_amount:40,refunded_amount:0});}});
  t.p._visible=true;t.p._loadVersion=1;t.p.setWorks([{jobId:'pending',status:'processing'},{jobId:'done',status:'processing'},{jobId:'older',status:'succeeded',result:'older.jpg'}]);
  await t.p.refreshPendingWorks();assert.deepEqual(statuses,['processing']);assert.equal(details.length,1);assert(details[0].endsWith('/done'));
  assert.equal(t.p.data.works.length,3);assert.equal(t.p.data.works.find(x=>x.jobId==='done').status,'succeeded');assert.equal(t.p.data.works.find(x=>x.jobId==='older').result,'older.jpg');
 });
 await test('works_expiry_is_based_on_server_expiry_not_created_date',()=>{
  const t=fixture('works');const near=t.p.mapCloudWork(job('near','succeeded',{expires_at:now/1000+2*86400,created_at:now/1000-29*86400}));
  const far=t.p.mapCloudWork(job('far','succeeded',{expires_at:now/1000+5*86400}));assert(near.expiryLabel.includes('2 天'));assert.equal(far.expiryLabel,'');
  const historical=t.p.mapCloudWork(job('old','failed',{charged_amount:null,refunded_amount:null}));assert(!historical.settlementLabel.includes('已退回 0'));
 });
 await test('works_bounded_completion_checks_rotate_past_unreachable_old_jobs',async()=>{
  const looked=[];const t=fixture('works',{myJobs:async()=>({jobs:[],processing_count:0,status_counts:counts}),request:async url=>{looked.push(url);if(!url.endsWith('/p6'))throw Error('temporarily missing');return job('p6');}});
  t.p._visible=true;t.p._loadVersion=1;t.p.setWorks(Array.from({length:7},(_,i)=>({jobId:'p'+i,status:'processing'})));
  await t.p.refreshPendingWorks();assert.equal(looked.length,6);await t.p.refreshPendingWorks();assert(looked.some(url=>url.endsWith('/p6')));assert.equal(t.p.data.works.find(w=>w.jobId==='p6').status,'succeeded');
 });
 await test('works_one_tap_save_downloads_owned_media_and_finishes_busy_state',async()=>{
  let saved;const t=fixture('works',{}, {saveImageToPhotosAlbum:o=>{saved=o.filePath;o.success({});}});t.p.setWorks([{jobId:'save',status:'succeeded',result:'signed.jpg'}]);
  await t.p.onSaveWork({currentTarget:{dataset:{index:0}}});assert.equal(saved,'wxfile://tmp/result.jpg');assert.equal(t.p.data.savingJobId,'');assert(t.messages.some(x=>x.title==='已保存到相册'));
 });
 await test('works_save_permission_denial_shows_settings_action_without_success',async()=>{
  const t=fixture('works',{}, {saveImageToPhotosAlbum:o=>o.fail({errMsg:'saveImageToPhotosAlbum:fail auth deny'})});t.p.setWorks([{jobId:'save',status:'succeeded',result:'signed.jpg'}]);
  await t.p.onSaveWork({currentTarget:{dataset:{index:0}}});assert(t.messages.some(x=>x.confirmText==='去设置'));assert(!t.messages.some(x=>x.title==='已保存到相册'));
 });
 await test('works_deep_link_locates_an_older_work_without_downloading_history',async()=>{
  const urls=[];const t=fixture('works',{request:async url=>{urls.push(url);return job('target');}});t.p._visible=true;t.p.onLoad({jobId:'target',status:'all'});
  await t.p.locateRequestedWork();assert.equal(urls.length,1);assert.equal(t.p.data.focusJobId,'target');assert.equal(t.p.data.works[0].jobId,'target');assert(t.messages.some(x=>x.selector==='#work-target'));
 });
 await test('works_recreate_delegates_to_single_shared_recipe_helper',async()=>{
  let id;const t=fixture('works',{}, {}, {},{recreateFromJob:async value=>{id=value;return true;}});await t.p.recreateWork({jobId:'recipe'});assert.equal(id,'recipe');assert(!t.p._recreating);
 });
 await test('community_paginates_first_screen_and_preserves_category_route',async()=>{
  const urls=[];const t=fixture('community',{request:async url=>{urls.push(url);return url.includes('offset=0')?{enabled:true,items:[{id:'one',category:'film',resultUrl:'one.jpg'}],total:2,has_more:true,next_offset:1}:
    {enabled:true,items:[{id:'one',category:'film',resultUrl:'one.jpg'},{id:'two',category:'film',resultUrl:'two.jpg'}],total:2,has_more:false,next_offset:3};}});
  await t.p.onSelectFilter({currentTarget:{dataset:{id:'film'}}});assert.equal(urls.length,1);assert(urls[0].includes('category=film'));await t.p.onReachBottom();assert.equal(t.p.data.items.length,2);assert.equal(urls.length,2);
 });
 await test('community_liked_filter_is_server_side_and_cannot_reuse_another_filter_response',async()=>{
  const finishes=[],urls=[];const t=fixture('community',{request:url=>{urls.push(url);return new Promise(r=>finishes.push(r));}});
  const a=t.p.loadCommunity(true),b=t.p.onSelectFilter({currentTarget:{dataset:{id:'liked'}}});assert.equal(urls.length,2);assert(urls[1].includes('liked_only=true'));
  finishes[1]({enabled:true,items:[{id:'liked',liked:true,resultUrl:'liked.jpg'}],has_more:false});await b;
  finishes[0]({enabled:true,items:[{id:'all',liked:false,resultUrl:'all.jpg'}]});await a;assert.equal(t.p.data.items[0].id,'liked');assert.equal(t.p.data.filteredItems.length,1);
 });
 await test('community_same_filter_merges_concurrent_refreshes',async()=>{
  let finish,calls=0;const t=fixture('community',{request:()=>{calls++;return new Promise(r=>finish=r);}});const a=t.p.loadCommunity(true),b=t.p.loadCommunity(true);assert.equal(calls,1);
  finish({enabled:true,items:[],has_more:false});await Promise.all([a,b]);assert(!t.p.data.loading);
 });
 await test('community_hidden_list_response_does_not_replace_cached_cards',async()=>{
  let finish;const t=fixture('community',{request:()=>new Promise(r=>finish=r)});t.p.data.items=[{id:'cached'}];const pending=t.p.loadCommunity(true);t.p.onHide();finish({enabled:true,items:[{id:'late',resultUrl:'late.jpg'}]});await pending;
  assert.equal(t.p.data.items[0].id,'cached');assert(!t.p.data.loading);assert(t.p._needsRefreshOnShow);
 });
 await test('template_preferences_intersect_existing_search_and_category_and_order_recent',async()=>{
  const t=fixture('templates',{}, {},{load:async()=>({template_favorites:['two'],recent_templates:['two','one']})});
  t.p._renderData([{id:'film',name:'胶片'}],[{id:'one',name:'胶片 一',group_id:'film'},{id:'two',name:'胶片 二',group_id:'film'},{id:'three',name:'人像',group_id:'portrait'}]);
  await t.p.loadPreferences();t.p.onPreferenceFilter({currentTarget:{dataset:{id:'favorites'}}});assert.equal(t.p.data.filteredTemplates[0].id,'two');
  t.p.onPreferenceFilter({currentTarget:{dataset:{id:'recent'}}});assert.equal(t.p.data.filteredTemplates[0].id,'two');
  t.p.onSearchInput({detail:{value:'一'}});assert.equal(t.p.data.filteredTemplates[0].id,'one');assert.equal(t.p.data.filteredTemplates.length,1);
 });
 await test('template_favorite_changes_only_after_server_success',async()=>{
  let fail=true;const t=fixture('templates',{}, {},{setFavorite:async()=>{if(fail)throw Error('offline');return {template_favorites:['one'],recent_templates:[]};}});
  t.p._renderData([],[{id:'one',name:'One'}]);await t.p.onFavorite({currentTarget:{dataset:{id:'one'}}});assert(!t.p.data.allTemplates[0].favorite);
  fail=false;await t.p.onFavorite({currentTarget:{dataset:{id:'one'}}});assert(t.p.data.allTemplates[0].favorite);
 });
 await test('profile_errors_do_not_turn_unknown_balance_or_works_into_zero_and_empty',async()=>{
  const t=fixture('my',{me:async()=>{throw Error('offline');},myJobs:async()=>{throw Error('offline');}});await t.p.refreshUserData(true);
  assert(!t.p.data.balanceLoaded);assert(!t.p.data.worksLoaded);assert(t.p.data.balanceError);assert(t.p.data.worksError);assert(!t.p.data.worksLoading);
  assert(xml('my').includes("balanceLoaded ? '✦ ' + lightPoints : '—'"));assert(xml('my').includes('worksLoading || !worksLoaded'));
 });
 await test('profile_recovers_real_record_count_and_processing_shortcut',async()=>{
  const t=fixture('my',{me:async()=>({balance:125,credit_record_count:7,earn:{}}),myJobs:async()=>({jobs:[],total:4,processing_count:2,has_more:true})});await t.p.refreshUserData(true);
  assert(t.p.data.balanceLoaded);assert(t.p.data.worksLoaded);assert.equal(t.p.data.lightPoints,125);assert.equal(t.p.data.creditRecordCount,7);t.p.onGoProcessing();assert(t.messages[0].url.endsWith('?status=processing'));
 });
 await test('preference_helper_uses_exact_authenticated_api_contract',async()=>{
  const calls=[],module={exports:{}};vm.runInNewContext(fs.readFileSync(path.join(root,'miniprogram/utils/preferences.js'),'utf8'),{module,require:()=>({request:async(url,options)=>{calls.push({url,options});return {template_favorites:['one','one'],recent_templates:['two']};}})});
  const p=await module.exports.load();assert.equal(p.template_favorites.length,1);await module.exports.setFavorite('one',false);await module.exports.recordRecent('two');
  assert.equal(calls[1].options.method,'PUT');assert.equal(calls[1].options.data.favorite,false);assert.equal(calls[2].url,'/api/me/recent-template');
 });
 await test('profile_cache_clear_removes_only_owned_draft_image_before_storage',async()=>{
  const storage={creationDraftV1:{owner:'user',input_mode:'photo',imagePath:'/user/creation_draft_1760000000000_abc123.jpg'}},files=new Set(['/user/creation_draft_1760000000000_abc123.jpg','/user/other.jpg']),events=[];
  const t=fixture('my',{}, {env:{USER_DATA_PATH:'/user'},getStorageSync:k=>storage[k],removeStorageSync:k=>{events.push('draft-key');delete storage[k];},getFileSystemManager:()=>({unlinkSync:p=>{events.push('draft-image');files.delete(p);}}),clearStorage:o=>{events.push('storage');Object.keys(storage).forEach(k=>delete storage[k]);o.success({});}});
  t.app.globalData.userId='user';t.app.onLaunch=()=>{};const module={exports:{}};
  vm.runInNewContext(fs.readFileSync(path.join(root,'miniprogram/utils/creation-draft.js'),'utf8'),{module,require:()=>t.api,wx:t.wx,getApp:()=>t.app,Date:Clock});Object.assign(t.creation,module.exports);
  t.p.onClearStorage();await t.messages[0].success({confirm:true});assert.deepEqual(events,['draft-key','draft-image','storage']);assert(files.has('/user/other.jpg'));assert(!files.has('/user/creation_draft_1760000000000_abc123.jpg'));
  assert(t.messages[0].content.includes('云端作品、模板收藏和光子余额不会删除'));
 });
 await test('profile_cache_clear_preserves_pending_submission_and_owned_draft',async()=>{
  let clears=0;const pending={client_request_id:'cr_pending'};const t=fixture('my',{}, {clearStorage:()=>clears++},{},{readPendingSubmission:()=>pending,clearDraft:async()=>clears++});
  t.p.onClearStorage();assert.equal(clears,0);assert.equal(t.messages[0].title,'先确认上次提交');assert.equal(pending.client_request_id,'cr_pending');
 });
 await test('profile_cache_clear_rechecks_pending_receipt_after_confirmation',async()=>{
  let pending=null,clears=0;const t=fixture('my',{}, {clearStorage:()=>clears++},{},{readPendingSubmission:()=>pending,clearDraft:async()=>clears++});
  t.p.onClearStorage();pending={client_request_id:'cr_late'};await t.messages[0].success({confirm:true});assert.equal(clears,0);assert.equal(t.messages[1].title,'先确认上次提交');
 });
 await test('retention_page_bindings_and_styles_are_well_formed',()=>{
  for(const name of ['works','community','templates','my','credits']){
   const t=fixture(name);new vm.Script(fs.readFileSync(path.join(root,`miniprogram/pages/${name}/${name}.js`),'utf8'));
   for(const m of xml(name).matchAll(/\b(?:bind|catch)\w+="([a-zA-Z_$][\w$]*)"/g))if(!['true','false'].includes(m[1]))assert.equal(typeof t.p[m[1]],'function',name+':'+m[1]);
   const css=fs.readFileSync(path.join(root,`miniprogram/pages/${name}/${name}.wxss`),'utf8');assert.equal((css.match(/\{/g)||[]).length,(css.match(/\}/g)||[]).length);
  }
 });
 const failed=cases.filter(x=>!x.passed).length;fs.mkdirSync(out,{recursive:true});fs.writeFileSync(path.join(out,'experience_retention_results.json'),JSON.stringify({cases,total:cases.length,failed},null,2),'utf8');
 console.log(`EXPERIENCE_RETENTION_SUMMARY total=${cases.length} passed=${cases.length-failed} failed=${failed}`);process.exitCode=failed?1:0;
})();
