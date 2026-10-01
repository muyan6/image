/* Offline page lifecycles, bounded cache, author actions and theme contracts. */
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const root=path.resolve(process.env.REVIEW_ROOT||path.resolve(__dirname,'../..'));
const out=path.resolve(process.env.REVIEW_OUTPUT||path.join(root,'audit/community-detail-ui')),cases=[];
function page(name,api={},wx={},app={globalData:{mediaCache:{}}}){
 let p;const messages=[];
 vm.runInNewContext(fs.readFileSync(path.join(root,`miniprogram/pages/${name}/${name}.js`),'utf8'),{
  Page:x=>p=x,require:()=>api,getApp:()=>app,console,setTimeout,clearTimeout,
  wx:{showToast:o=>messages.push(o),showModal:o=>messages.push(o),showActionSheet:o=>messages.push(o),
    navigateTo:o=>messages.push(o),switchTab:o=>messages.push(o),hideKeyboard(){},stopPullDownRefresh(){},...wx}});
 p.data=JSON.parse(JSON.stringify(p.data));p.setData=d=>Object.assign(p.data,d);return {p,app,messages};
}
const post={id:'s_post',title:'色彩灵感',story:'创作故事',resultUrl:'https://cos.invalid/result.jpg?q-signature=abc',origUrl:'',likes:0,liked:false,templateId:'t_film',templateName:'胶片'};
const empty={items:[],total:0,has_more:false,next_offset:0};
async function test(name,fn){try{await fn();cases.push({case:name,passed:true});}catch(e){console.error(name,e);cases.push({case:name,passed:false,error:e.stack});}}
function apiModule(wx,app){let module={exports:{}};vm.runInNewContext(fs.readFileSync(path.join(root,'miniprogram/utils/api.js'),'utf8'),{module,getApp:()=>app,wx,console,setTimeout,clearTimeout});return module.exports;}
(async()=>{
 await test('detail_route_and_community_card_navigation_preserve_theme',()=>{
  assert(JSON.parse(fs.readFileSync(path.join(root,'miniprogram/app.json'))).pages.includes('pages/community-detail/community-detail'));
  const t=page('community');t.p.data.items=[post];t.p.onOpenPost({currentTarget:{dataset:{id:post.id}}});
  assert.equal(t.app.globalData.communityPreview.id,post.id);assert(t.messages[0].url.endsWith('?id=s_post'));
  const xml=fs.readFileSync(path.join(root,'miniprogram/pages/community/community.wxml'),'utf8');
  assert(xml.includes('bindtap="onOpenPost"'));assert(xml.includes('catchtap="onLikeItem"'));assert(xml.includes('catchtap="onMakeSame"'));
  const css=fs.readFileSync(path.join(root,'miniprogram/pages/community-detail/community-detail.wxss'),'utf8');
  for(const token of ['var(--paper-bg)','var(--accent-gold)','var(--font-serif)'])assert(css.includes(token));
 });
 await test('tab_return_uses_recent_data_without_force_refresh',async()=>{
  let calls=0;const t=page('community',{absolute:x=>x,request:async()=>{calls++;return {enabled:true,items:[post]};}});
  await t.p.loadCommunity();t.p.onShow();t.p.onShow();assert.equal(calls,1);assert.equal(t.p.data.items.length,1);
 });
 await test('background_refresh_keeps_cards_and_merges_one_inflight_request',async()=>{
  let done,calls=0;const t=page('community',{absolute:x=>x,request:()=>{calls++;return new Promise(r=>done=r);}});
  t.p.data.items=[post];const a=t.p.loadCommunity(true),b=t.p.loadCommunity(true);assert.equal(calls,1);assert(!t.p.data.loading);
  done({enabled:true,items:[post]});await Promise.all([a,b]);
 });
 await test('detail_delta_updates_list_on_return',()=>{
  const app={globalData:{communityUpdated:{id:post.id,likes:3,liked:true,comments:4}}};const t=page('community',{}, {},app);
  t.p.data.items=[post];t.p.onShow();assert.equal(t.p.data.items[0].comments,4);assert.equal(t.p.data.items[0].likes,3);
 });
 await test('cos_signature_refresh_reuses_existing_local_image',async()=>{
  let fetches=0;const app={globalData:{mediaCache:{},apiBase:'https://api.invalid'}};
  const api=apiModule({getFileSystemManager:()=>({accessSync(){}}),getImageInfo:o=>{fetches++;o.success({path:'wxfile://tmp/one.jpg'});}},app);
  const first=post.resultUrl,next=first.replace('abc','def');
  await api.rememberCommunityImage(first);await api.rememberCommunityImage(next);
  assert.equal(fetches,1);assert.equal(api.communityImage(next),'wxfile://tmp/one.jpg');
 });
 await test('cache_limit_missing_file_and_editorial_query_identity',async()=>{
  let exists=true,fetches=0;const app={globalData:{mediaCache:{},apiBase:'https://api.invalid'}};
  const api=apiModule({getFileSystemManager:()=>({accessSync(){if(!exists)throw Error('gone');}}),getImageInfo:o=>{fetches++;o.success({path:'wxfile://tmp/'+fetches+'.jpg'});}},app);
  for(let n=0;n<40;n++)await api.rememberCommunityImage('https://cos.invalid/'+n+'.jpg?q-signature=x');
  assert.equal(Object.keys(app.globalData.mediaCache.__community).length,36);
  exists=false;assert.equal(api.communityImage('https://cos.invalid/39.jpg?q-signature=y'),'https://cos.invalid/39.jpg?q-signature=y');exists=true;
  await api.rememberCommunityImage('https://images.invalid/view?id=1');await api.rememberCommunityImage('https://images.invalid/view?id=2');assert.equal(fetches,42);
 });
 await test('simultaneous_image_cache_requests_are_coalesced',async()=>{
  let fetches=0,done;const app={globalData:{mediaCache:{},apiBase:'https://api.invalid'}};
  const api=apiModule({getFileSystemManager:()=>({accessSync(){}}),getImageInfo:o=>{fetches++;done=o.success;}},app);
  const a=api.rememberCommunityImage(post.resultUrl),b=api.rememberCommunityImage(post.resultUrl);done({path:'wxfile://tmp/a.jpg'});await Promise.all([a,b]);assert.equal(fetches,1);
 });
 await test('post_loading_and_comment_list_bind_source_data',async()=>{
  const t=page('community-detail',{absolute:x=>x,request:async u=>u.includes('/comments')?{...empty,items:[{id:'c_one',content:'好看',created_at:1}],total:1}:{post}});
  await t.p.onLoad({id:post.id});assert.equal(t.p.data.post.title,post.title);assert.equal(t.p.data.comments[0].content,'好看');assert.equal(t.app.globalData.communityUpdated.comments,1);
 });
 await test('deleted_post_clears_preview_instead_of_leaving_interactions',async()=>{
  const app={globalData:{communityPreview:post}},err=Object.assign(new Error('帖子已下架'),{status:404});
  const t=page('community-detail',{request:async()=>{throw err;}},{},app);await t.p.onLoad({id:post.id});
  assert.equal(t.p.data.post,null);assert(t.p.data.unavailable);assert(app.globalData.communityDirty);
 });
 await test('comment_error_preserves_draft_and_retry_ticket',async()=>{
  let sent=[];const t=page('community-detail',{request:async(u,o)=>{sent.push(o.data);throw Object.assign(Error('审核超时'),{status:503});}});
  t.p.data.post=post;t.p._id=post.id;t.p.data.draft='我的留言';await t.p.onSend();await t.p.onSend();
  assert.equal(t.p.data.draft,'我的留言');assert(t.p.data.sendError);assert.equal(sent[0].request_id,sent[1].request_id);assert(!t.p.data.sending);
 });
 await test('double_send_is_blocked_and_success_clears_draft',async()=>{
  let done,calls=0;const t=page('community-detail',{request:(u,o)=>{if(!o)return Promise.resolve(empty);calls++;return new Promise(r=>done=r);}});
  t.p.data.post=post;t.p._id=post.id;t.p.data.draft='留言';const sending=t.p.onSend();await t.p.onSend();assert.equal(calls,1);done({ok:true});await sending;assert.equal(t.p.data.draft,'');assert(!t.p.data.sending);
 });
 await test('likes_reports_and_own_deletion_use_persistent_endpoints',async()=>{
  let requests=[];const t=page('community-detail',{request:async(u,o)=>{requests.push({u,o});return o?{likes:2,liked:true}:empty;}});
  t.p._id=post.id;t.p.data.post=post;t.p.data.comments=[{id:'c_one',liked:false}];
  await t.p.onLikePost();await t.p.onLikeComment({currentTarget:{dataset:{id:'c_one'}}});assert.equal(t.p.data.comments[0].likes,2);
  t.p.onReport({currentTarget:{dataset:{kind:'comment',id:'c_one'}}});await t.messages.at(-1).success({tapIndex:0});
  assert(requests.some(x=>x.u==='/api/community/comments/c_one/report'&&x.o.data.reason==='spam'));
  t.p.onDeleteComment({currentTarget:{dataset:{id:'c_one'}}});await t.messages.at(-1).success({confirm:true});assert(requests.some(x=>x.o&&x.o.method==='DELETE'));
 });
 await test('detail_share_keeps_post_id_and_remote_image',()=>{
  const t=page('community-detail');t.p._id=post.id;t.p.data.post={...post,resultUrl:'wxfile://tmp/a',resultRemote:post.resultUrl};
  const share=t.p.onShareAppMessage();assert(share.path.endsWith('?id=s_post'));assert.equal(share.imageUrl,post.resultUrl);t.p.onMakeSame();assert.equal(t.app.globalData.selectedTemplate.id,'t_film');
 });
 await test('invite_records_paginate_with_actual_historical_amount',async()=>{
  const t=page('invite',{request:async u=>u.endsWith('offset=0')?{items:[{id:'a',nickname:'好友',reward:45,bound_at:1}],total:2,recorded_reward:45,historical_unknown:1,next_offset:1,has_more:true}:{items:[{id:'b',reward:null,bound_at:2}],total:2,recorded_reward:45,historical_unknown:1,next_offset:2,has_more:false}});
  await t.p.loadRecords();await t.p.onMoreRecords();assert.equal(t.p.data.records.length,2);assert.equal(t.p.data.recordedReward,45);assert.equal(t.p.data.records[1].reward,null);
 });
 await test('invite_errors_are_retryable_and_not_fake_empty',async()=>{
  const t=page('invite',{request:async()=>{throw Error('offline');}});await t.p.loadRecords();assert(t.p.data.recordsError);assert(!t.p.data.recordsLoaded);assert(!t.p.data.recordsLoading);
 });
 await test('privacy_retention_and_credits_layout_fix_contracts',()=>{
  const privacy=fs.readFileSync(path.join(root,'miniprogram/pages/privacy/privacy.wxml'),'utf8');assert.equal((privacy.match(/class="terms-card gallery-card"/g)||[]).length,1);assert.equal((privacy.match(/class="term-num"/g)||[]).length,4);
  const retention=fs.readFileSync(path.join(root,'miniprogram/pages/retention/retention.wxml'),'utf8');assert(retention.includes('class="tips-box gallery-card"><view class="tip-item"><text class="tip-h">社区投稿独立保存'));
  const credits=fs.readFileSync(path.join(root,'miniprogram/pages/credits/credits.wxml'),'utf8');assert(!credits.includes('个人主体支付规则'));
  const invite=fs.readFileSync(path.join(root,'miniprogram/pages/invite/invite.wxml'),'utf8');assert(!invite.includes('暂不展示邀请明细'));assert(invite.includes('item.reward === null'));
 });
 await test('comment_paths_auto_login_and_keep_authorization',async()=>{
  let auth;const app={globalData:{apiBase:'https://api.invalid'}};
  const api=apiModule({getStorageSync:()=> 'fixture-token',request:o=>{auth=o.header.Authorization;o.success({statusCode:200,data:{ok:true}});}},app);
  await api.request('/api/community/comments/c_one/report',{method:'POST',data:{reason:'spam'}});assert.equal(auth,'Bearer fixture-token');
 });
 await test('all_new_wxml_events_exist_and_files_parse',()=>{
  for(const name of ['community-detail','invite','community']){
   const t=page(name),xml=fs.readFileSync(path.join(root,`miniprogram/pages/${name}/${name}.wxml`),'utf8');
   for(const match of xml.matchAll(/(?:bind|catch)(?:tap|input|confirm|load|error|keyboardheightchange)="(\w+)"/g))assert.equal(typeof t.p[match[1]],'function',name+':'+match[1]);
   JSON.parse(fs.readFileSync(path.join(root,`miniprogram/pages/${name}/${name}.json`),'utf8'));
  }
 });
 await test('community_cta_is_compact_right_aligned_and_keeps_action',()=>{
  const css=fs.readFileSync(path.join(root,'miniprogram/pages/community/community.wxss'),'utf8');
  const rule=css.match(/\.exhibit-foot \.make-same-btn\s*\{([^}]+)\}/);assert(rule);
  for(const value of ['margin: 0','width: 264rpx','height: 60rpx','font-size: 23rpx','line-height: 1','flex: 0 0 264rpx'])assert(rule[1].includes(value),value);
  assert(css.includes('min-width: 0'));assert(css.includes('gap: 20rpx'));
  const xml=fs.readFileSync(path.join(root,'miniprogram/pages/community/community.wxml'),'utf8');assert(xml.includes('catchtap="onMakeSame"'));assert(xml.includes('data-template-id="{{ item.templateId }}"'));
  const t=page('community');t.p.onMakeSame({currentTarget:{dataset:{templateId:'t_film',name:'胶片'}}});assert.equal(t.app.globalData.selectedTemplate.id,'t_film');
 });
 fs.mkdirSync(out,{recursive:true});fs.writeFileSync(path.join(out,'community_detail_frontend_results.json'),JSON.stringify({cases},null,2));
 console.log(`COMMUNITY_DETAIL_FRONTEND_SUMMARY total=${cases.length} passed=${cases.filter(x=>x.passed).length} failed=${cases.filter(x=>!x.passed).length}`);
 process.exit(cases.every(x=>x.passed)?0:1);
})();
