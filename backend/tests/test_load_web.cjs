/* Offline request-count, real DOM and race regression tests for display-only web optimizations. */
const fs=require('fs'),path=require('path'),assert=require('assert'),{pathToFileURL}=require('url');
const {JSDOM}=require(process.env.WEB_DOM_MODULE||'jsdom');
const root=path.resolve(process.env.REVIEW_ROOT||path.join(__dirname,'../..'));
const out=path.resolve(process.env.REVIEW_OUTPUT||path.join(root,'audit/2026-10-02/load-optimization/web-load-tests'));
const tick=()=>new Promise(resolve=>setImmediate(resolve)),settle=async()=>{for(let i=0;i<6;i++)await tick();};
const cases=[],measurements={};
async function test(name,fn){try{await fn();cases.push({case:name,passed:true});console.log('PASS '+name);}catch(error){cases.push({case:name,passed:false,error:error.stack});console.error('FAIL '+name+': '+error.message);}}
const auth=(id='alpha')=>({auth_source:'site',user:{user_id:'CANONICAL_'+id,account_user_id:'SITE_'+id,username:id,nickname:id},balance:200});
const config={prices:{light:40,fine:80},free_mode:false,cos_ready:true,template_quality_options:['light','fine'],template_output_modes:['template','single'],text_generation:{ready:true,price:40}};
function signed(key,{end=Math.floor(Date.now()/1000)+3600,start=Math.floor(Date.now()/1000)-30,signature='fixture',transform=''}={}){return `https://fixture.myqcloud.com/${key}?${transform?transform+'&':''}q-sign-time=${start}%3B${end}&q-signature=${signature}`;}
const clone=value=>JSON.parse(JSON.stringify(value));
async function fixture({session=auth(),route='community',handler,creationOptions,withCreation=false}={}){
 const {createApplication}=await import(pathToFileURL(path.join(root,'backend/static/web/app.js')).href);
 const dom=new JSDOM('<!doctype html><main id="app"></main>',{url:'https://site.invalid/',pretendToBeVisual:true}),doc=dom.window.document,calls=[];
 dom.window.HTMLDialogElement.prototype.showModal=function(){this.open=true;};dom.window.HTMLDialogElement.prototype.close=function(){this.open=false;};
 const store={templates:{groups:[],items:[{id:'tpl',name:'模板',cover:signed('cover.jpg'),thumbnail:signed('cover.jpg',{transform:'imageMogr2%2Fthumbnail%2F480x480'})}]},jobs:[{id:'job',status:'succeeded',stage:'finalize',created_at:1,expires_at:Math.floor(Date.now()/1000)+3600,result_url:signed('full.jpg'),thumb_url:signed('full.jpg',{transform:'imageMogr2%2Fthumbnail%2F480x480'})}],posts:[{id:'post',title:'公开作品',authorName:'作者',story:'故事',likes:0,liked:false,category:'all',resultUrl:signed('post.jpg'),thumbnailUrl:signed('post.jpg',{transform:'imageMogr2%2Fthumbnail%2F480x480'})}]};
 const response=(data,status=200)=>({ok:status>=200&&status<300,status,json:async()=>clone(data)});
 const fetch=async(url,init)=>{
  const method=init.method||'GET',request={url,method,body:init.body&&JSON.parse(init.body),signal:init.signal};calls.push(request);
  if(handler){const result=await handler(request,store,response);if(result!==undefined)return result;}
  if(url==='/api/auth/site/session')return session?response(session):response({detail:'guest'},401);
  if(url==='/api/auth/site/login')return response(auth(request.body.username));
  if(url==='/api/auth/site/logout')return response({ok:true});
  if(url==='/api/config')return response(config);
  if(url==='/api/me')return response({user_id:'CANONICAL',balance:200,nickname:'真实昵称'});
  if(url==='/api/templates')return response(store.templates);
  if(url==='/api/me/preferences')return response({template_favorites:[],recent_templates:[]});
  if(url.startsWith('/api/my/jobs?')){const u=new URL(url,'https://site.invalid'),status=u.searchParams.get('status'),jobs=status==='processing'?store.jobs.filter(j=>j.status==='processing'):store.jobs;return response({jobs,total:jobs.length,next_offset:jobs.length,has_more:false,status_counts:{all:store.jobs.length,processing:store.jobs.filter(j=>j.status==='processing').length}});}
  if(url.startsWith('/api/my/jobs/')&&method==='DELETE'){store.jobs=store.jobs.filter(j=>j.id!==url.split('/').pop());return response({ok:true});}
  if(url.startsWith('/api/jobs/')){const id=url.split('/')[3],row=store.jobs.find(j=>j.id===id);return row?response(row):response({detail:'已到期'},404);}
  if(url.startsWith('/api/community?'))return response({items:store.posts,next_offset:store.posts.length,has_more:false});
  if(url==='/api/community/posts/post/like'){Object.assign(store.posts[0],{liked:request.body.liked,likes:request.body.liked?1:0});return response({id:'post',liked:store.posts[0].liked,likes:store.posts[0].likes});}
  if(url==='/api/community/posts/post')return response({post:store.posts[0]});
  if(url.includes('/comments?'))return response({items:[],next_offset:0,has_more:false});
  return response({ok:true});
 };
 const options={document:doc,fetch,disableHistory:true,initialRoute:route,confirm:()=>true,creationOptions};
 if(!withCreation)options.mountCreation=async()=>()=>{};
 const app=createApplication(options);await app.start();await settle();
 return {app,dom,doc,calls,store,finish(){app.destroy();dom.window.close();}};
}
function click(f,selector){const node=f.doc.querySelector(selector);assert(node,'Missing '+selector);node.click();return node;}
function visibility(f,state){Object.defineProperty(f.doc,'visibilityState',{configurable:true,value:state});f.doc.dispatchEvent(new f.dom.window.Event('visibilitychange'));}
function fakeClock(){
 const original={now:Date.now,set:setTimeout,clear:clearTimeout},pending=new Map(),cleared=[];let now=Date.now(),serial=0;
 Date.now=()=>now;global.setTimeout=(fn,ms)=>{const id=++serial;pending.set(id,{fn,ms});return id;};global.clearTimeout=id=>{cleared.push(id);pending.delete(id);};
 return {pending,cleared,fire(id){const item=pending.get(id);assert(item,'Timer not found');pending.delete(id);now+=item.ms;item.fn();},restore(){Date.now=original.now;global.setTimeout=original.set;global.clearTimeout=original.clear;}};
}
(async()=>{
 const helpers=await import(pathToFileURL(path.join(root,'backend/static/web/shared-load.js')).href);
 const {CreationSession}=await import(pathToFileURL(path.join(root,'backend/static/web/creation.js')).href);
 await test('public_catalog_merges_inflight_and_reuses_30_second_result',async()=>{
  let release,loads=0,now=0;const cache=helpers.createDisplayCache({now:()=>now}),loader=()=>{loads++;return new Promise(resolve=>release=resolve);};
  const a=cache.get('public:\n/api/templates',loader),b=cache.get('public:\n/api/templates',loader);await tick();assert.equal(loads,1);release({items:[{id:'one'}]});await Promise.all([a,b]);
  const copy=await cache.get('public:\n/api/templates',loader);copy.items[0].id='changed';assert.equal((await cache.get('public:\n/api/templates',loader)).items[0].id,'one');
  now=30001;const c=cache.get('public:\n/api/templates',loader);await tick();assert.equal(loads,2);release({items:[]});await c;measurements.catalogRepeatedRequests={before:2,after:1};
 });
 await test('manual_refresh_bypasses_cache_and_cache_is_bounded',async()=>{
  const cache=helpers.createDisplayCache({maxEntries:3});let loads=0;await cache.get('public:\n/api/templates',()=>({count:++loads}));await cache.get('public:\n/api/templates',()=>({count:++loads}),{force:true});assert.equal(loads,2);
  for(let i=0;i<10;i++)await cache.get('private:'+i,()=>({id:i}));assert.equal(cache.size,3);
 });
 await test('mutation_invalidates_inflight_display_and_never_resurrects_deleted_rows',async()=>{
  const cache=helpers.createDisplayCache();let release;const pending=cache.get('private:SITE\n/api/my/jobs?limit=24',()=>new Promise(resolve=>release=resolve));await tick();cache.invalidate('private:');release({jobs:[{id:'deleted'}]});await assert.rejects(pending,error=>error.code==='DISPLAY_STALE');assert.equal(cache.size,0);
 });
 await test('expiry_rejects_cached_signed_urls_and_deleted_retention_dates',async()=>{
  let now=Date.now(),loads=0;const cache=helpers.createDisplayCache({now:()=>now}),old=signed('a.jpg',{end:Math.floor(now/1000)+15});
  await cache.get('private:\n/api/my/jobs',()=>({jobs:[{id:'one',status:'succeeded',result_url:old,expires_at:Math.floor(now/1000)+15}],loads:++loads}));
  now+=16000;const next=await cache.get('private:\n/api/my/jobs',()=>({jobs:[],loads:++loads}));assert.equal(next.loads,2);assert.equal(next.jobs.length,0);
  assert(!helpers.mediaReusable(signed('expired.jpg',{end:1})));assert(!helpers.mediaReusable('https://cos.invalid/a?q-signature=no-timestamp'));
 });
 await test('valid_media_stays_stable_without_discarding_transform_or_editorial_version',()=>{
  const previous=signed('a.jpg',{signature:'old',transform:'imageMogr2%2Fthumbnail%2F480x480'}),fresh=signed('a.jpg',{signature:'new',transform:'imageMogr2%2Fthumbnail%2F480x480'});
  assert.equal(helpers.stableMediaUrl(fresh,previous),previous);assert.equal(helpers.stableMediaUrl(signed('a.jpg',{transform:'imageMogr2%2Fthumbnail%2F960x960'}),previous),signed('a.jpg',{transform:'imageMogr2%2Fthumbnail%2F960x960'}));
  assert.equal(helpers.stableMediaUrl('/image.jpg?v=2','/image.jpg?v=1'),'/image.jpg?v=2');
 });
 await test('cache_refresh_keeps_valid_same_object_signatures_across_route_remount',async()=>{
  const cache=helpers.createDisplayCache(),old=signed('a.jpg',{signature:'old'}),fresh=signed('a.jpg',{signature:'new'});
  await cache.get('private:\n/api/my/jobs',()=>({jobs:[{id:'one',result_url:old}]}));const data=await cache.get('private:\n/api/my/jobs',()=>({jobs:[{id:'one',result_url:fresh}]}),{force:true});assert.equal(data.jobs[0].result_url,old);
 });
 await test('real_app_returns_to_public_catalog_and_works_without_repeated_get',async()=>{
  const f=await fixture({route:'works'});await f.app.navigate('privacy');await f.app.navigate('works');await settle();assert.equal(f.calls.filter(c=>c.url.startsWith('/api/my/jobs?')).length,1);
  await f.app.ctx.displayGet('/api/templates',{public:true});await f.app.ctx.displayGet('/api/templates',{public:true});assert.equal(f.calls.filter(c=>c.url==='/api/templates').length,1);f.finish();
 });
 await test('real_app_manual_refresh_is_real_get_and_never_cached_balance_or_price',async()=>{
  const f=await fixture({route:'works'});click(f,'.lib-link');await settle();assert.equal(f.calls.filter(c=>c.url.startsWith('/api/my/jobs?')).length,2);
  const before=f.calls.length;await f.app.ctx.displayGet('/api/me');await f.app.ctx.displayGet('/api/me');await f.app.ctx.displayGet('/api/config');await f.app.ctx.displayGet('/api/config');assert.equal(f.calls.length-before,4);f.finish();
 });
 await test('unchanged_card_and_image_nodes_survive_refresh_and_signature_rotation',async()=>{
  const f=await fixture({route:'works'}),card=f.doc.querySelector('.work-card'),img=card.querySelector('img'),old=img.src;f.store.jobs[0].result_url=signed('full.jpg',{signature:'rotated'});f.store.jobs[0].thumb_url=signed('full.jpg',{signature:'rotated',transform:'imageMogr2%2Fthumbnail%2F480x480'});
  click(f,'.lib-link');await settle();assert.strictEqual(f.doc.querySelector('.work-card'),card);assert.strictEqual(f.doc.querySelector('.work-card img'),img);assert.equal(img.src,old);measurements.unchangedWorkCardRetained=true;f.finish();
 });
 await test('mutated_card_retains_unchanged_image_and_other_cards',async()=>{
  const f=await fixture(),img=f.doc.querySelector('.community-card img'),card=f.doc.querySelector('.community-card');click(f,'[data-action="like-post"]');await settle();assert.notStrictEqual(f.doc.querySelector('.community-card'),card);assert.strictEqual(f.doc.querySelector('.community-card img'),img);assert(f.doc.body.textContent.includes('已喜欢'));f.finish();
 });
 await test('thumbnail_is_list_only_detail_keeps_full_original_metadata',async()=>{
  const f=await fixture({route:'works'});assert.equal(f.doc.querySelector('.work-card img').src,f.store.jobs[0].thumb_url);click(f,'.lib-photo-button');await settle();assert.equal(f.doc.querySelector('dialog img').src,f.store.jobs[0].result_url);f.finish();
 });
 await test('thumbnail_error_refreshes_json_once_then_falls_back_to_cos_full',async()=>{
  const f=await fixture({route:'works'}),img=f.doc.querySelector('.work-card img');f.store.jobs[0].thumb_url=signed('new-thumb.jpg');img.dispatchEvent(new f.dom.window.Event('error'));await settle();assert.equal(img.src,f.store.jobs[0].thumb_url);img.dispatchEvent(new f.dom.window.Event('error'));await settle();assert.equal(img.src,f.store.jobs[0].result_url);assert.equal(f.calls.filter(c=>c.url==='/api/jobs/job').length,1);assert(!f.calls.some(c=>c.url.startsWith('/api/images/')));f.finish();
 });
 await test('thumbnail_metadata_404_removes_card_and_does_not_revive_on_return',async()=>{
  const f=await fixture({route:'works'}),img=f.doc.querySelector('.work-card img');f.store.jobs=[];img.dispatchEvent(new f.dom.window.Event('error'));await settle();assert.equal(f.doc.querySelectorAll('.work-card').length,0);await f.app.navigate('privacy');await f.app.navigate('works');await settle();assert.equal(f.doc.querySelectorAll('.work-card').length,0);f.finish();
 });
 await test('delete_mutation_invalidates_return_cache_without_paid_post',async()=>{
  const f=await fixture({route:'works'});click(f,'[data-action="delete-work"]');await settle();await f.app.navigate('privacy');await f.app.navigate('works');await settle();assert.equal(f.doc.querySelectorAll('.work-card').length,0);assert.equal(f.calls.filter(c=>c.method==='DELETE').length,1);assert.equal(f.calls.filter(c=>c.method==='POST'&&/text-generation|rescue/.test(c.url)).length,0);f.finish();
 });
 await test('logout_and_different_stable_account_do_not_share_private_cached_likes',async()=>{
  const f=await fixture();assert.equal(f.calls.filter(c=>c.url.startsWith('/api/community?')).length,1);await f.app.logout();await f.app.navigate('community');await settle();assert.equal(f.calls.filter(c=>c.url.startsWith('/api/community?')).length,2);
  const pending=f.app.requireLogin(),form=f.doc.querySelector('dialog form');f.doc.querySelector('[aria-label="用户名"]').value='beta';f.doc.querySelector('[aria-label="密码"]').value='fixture-password';form.dispatchEvent(new f.dom.window.Event('submit',{bubbles:true,cancelable:true}));await pending;await settle();assert.equal(f.app.state.user.account_user_id,'SITE_beta');await f.app.navigate('community');await settle();assert(f.calls.filter(c=>c.url.startsWith('/api/community?')).length>=3);f.finish();
 });
 await test('text_mount_skips_catalog_and_template_page_reuses_public_display',async()=>{
  const f=await fixture({route:'text',withCreation:true,session:null});assert.equal(f.calls.filter(c=>c.url==='/api/templates').length,0);await f.app.navigate('templates');await settle();await f.app.navigate('create');await settle();assert.equal(f.calls.filter(c=>c.url==='/api/templates').length,1);assert.equal(f.doc.querySelector('input[type="file"]').type,'file');measurements.textBootCatalogRequests={before:1,after:0};f.finish();
 });
 await test('cached_selected_template_is_revalidated_before_any_paid_upload',async()=>{
  const f=await fixture();const s=new CreationSession(f.app.ctx,{receipts:{read:()=>null},drafts:{}});await s.catalog('tpl');f.store.templates.items=[];await assert.rejects(s.catalog('tpl',true),error=>error.code==='TEMPLATE_MISSING');assert(!f.calls.some(c=>c.url==='/api/uploads'));s.close();f.finish();
 });
 await test('creation_poll_hidden_has_zero_requests_and_restore_is_immediate',async()=>{
  const dom=new JSDOM('<main></main>',{pretendToBeVisual:true});let requests=0,ticks=0;const ctx={accountVersion:1,state:{user:{account_user_id:'SITE'},config},document:dom.window.document,api:async()=>{requests++;return {status:'succeeded',stage:'finalize'};}};
  Object.defineProperty(ctx.document,'visibilityState',{configurable:true,value:'hidden'});const s=new CreationSession(ctx,{receipts:{},drafts:{}}),pending=s.poll('job',()=>ticks++);await settle();assert.equal(requests,0);Object.defineProperty(ctx.document,'visibilityState',{configurable:true,value:'visible'});ctx.document.dispatchEvent(new dom.window.Event('visibilitychange'));await pending;assert.equal(requests,1);assert.equal(ticks,1);measurements.hiddenGenerationPollRequests=0;s.close();dom.window.close();
 });
 await test('creation_poll_restores_after_five_hidden_minutes_within_absolute_cap',async()=>{
  const dom=new JSDOM('<main></main>',{pretendToBeVisual:true});let requests=0;const original=Date.now;let now=original();Date.now=()=>now;
  try{const ctx={accountVersion:1,state:{user:null,config},document:dom.window.document,api:async()=>{requests++;return {status:'succeeded'};}};Object.defineProperty(ctx.document,'visibilityState',{configurable:true,value:'hidden'});const s=new CreationSession(ctx,{receipts:{},drafts:{}}),pending=s.poll('job',()=>{});now+=5*60*1000;Object.defineProperty(ctx.document,'visibilityState',{configurable:true,value:'visible'});ctx.document.dispatchEvent(new dom.window.Event('visibilitychange'));await pending;assert.equal(requests,1);s.close();}finally{Date.now=original;dom.window.close();}
 });
 await test('creation_poll_ignores_unchanged_stage_and_backs_off_after_one_minute',async()=>{
  const dom=new JSDOM('<main></main>',{pretendToBeVisual:true});const original=Date.now;let now=100000,requests=0,ticks=0;const delays=[];Date.now=()=>now;
  try{const ctx={accountVersion:1,state:{user:null,config},document:dom.window.document,api:async()=>({status:++requests===4?'succeeded':'processing',stage:requests===4?'finalize':'enhance'})};const s=new CreationSession(ctx,{receipts:{},drafts:{},delay:async ms=>{delays.push(ms);now+=requests===1?61000:ms;}});await s.poll('job',()=>ticks++);assert.equal(ticks,2);assert.deepEqual(delays,[1500,5000,5000]);s.close();}finally{Date.now=original;dom.window.close();}
 });
 await test('creation_poll_transient_errors_back_off_but_never_repeat_generation',async()=>{
  const dom=new JSDOM('<main></main>',{pretendToBeVisual:true});let requests=0;const delays=[],ctx={accountVersion:1,state:{user:null,config},document:dom.window.document,api:async(url,settings)=>{assert.equal(settings&&settings.method,undefined);assert(url==='/api/jobs/job');if(++requests<=5)throw Object.assign(new Error('network'),{status:503});return {status:'succeeded'};}};
  const s=new CreationSession(ctx,{receipts:{},drafts:{},delay:async ms=>delays.push(ms)});await s.poll('job',()=>{});assert.deepEqual(delays,[3000,6000,12000,15000,15000]);s.close();dom.window.close();
 });
 await test('hidden_inflight_poll_aborts_read_then_resumes_without_paid_post',async()=>{
  const dom=new JSDOM('<main></main>',{pretendToBeVisual:true});let calls=0,firstSignal;const ctx={accountVersion:1,state:{user:null,config},document:dom.window.document,api:(url,settings)=>{calls++;assert.equal(url,'/api/jobs/job');if(calls===1){firstSignal=settings.signal;return new Promise((resolve,reject)=>settings.signal.addEventListener('abort',()=>reject(new Error('abort'))));}return Promise.resolve({status:'succeeded'});}};
  const s=new CreationSession(ctx,{receipts:{},drafts:{}}),pending=s.poll('job',()=>{});Object.defineProperty(ctx.document,'visibilityState',{configurable:true,value:'hidden'});ctx.document.dispatchEvent(new dom.window.Event('visibilitychange'));await settle();assert(firstSignal.aborted);assert.equal(calls,1);Object.defineProperty(ctx.document,'visibilityState',{configurable:true,value:'visible'});ctx.document.dispatchEvent(new dom.window.Event('visibilitychange'));await pending;assert.equal(calls,2);s.close();dom.window.close();
 });
 await test('creation_stalled_get_aborts_at_fifteen_seconds_and_clears_read_timer',async()=>{
  const dom=new JSDOM('<main></main>',{pretendToBeVisual:true}),clock=fakeClock();let signal,requests=0,s;
  try{const ctx={accountVersion:1,state:{user:null,config},document:dom.window.document,api:(url,settings)=>{requests++;assert.equal(url,'/api/jobs/job');assert.equal(settings.method,undefined);signal=settings.signal;return new Promise((resolve,reject)=>signal.addEventListener('abort',()=>reject(new Error('read timeout'))));}};
   s=new CreationSession(ctx,{receipts:{},drafts:{},delay:async()=>s.close()});const pending=s.poll('job',()=>{}),entry=[...clock.pending.entries()][0];assert.equal(entry[1].ms,15000);clock.fire(entry[0]);await pending;assert(signal.aborted);assert.equal(requests,1);assert(clock.cleared.includes(entry[0]));assert.equal(clock.pending.size,0);
  }finally{if(s)s.close();clock.restore();dom.window.close();}
 });
 await test('read_timeout_clips_to_remaining_absolute_deadline',()=>{
  const clock=fakeClock();try{const controller=new AbortController(),cancel=helpers.armReadTimeout(controller,Date.now()+700);const entry=[...clock.pending.entries()][0];assert.equal(entry[1].ms,700);clock.fire(entry[0]);assert(controller.signal.aborted);cancel();assert.equal(clock.pending.size,0);}finally{clock.restore();}
 });
 await test('works_stalled_get_aborts_at_fifteen_seconds_without_timer_or_post_leak',async()=>{
  const clock=fakeClock();let f,signal;
  try{f=await fixture({route:'works',handler:(request,store)=>{if(request.url==='/api/auth/site/session')store.jobs[0]={id:'job',status:'processing',stage:'enhance'};if(request.url.includes('status=processing')){signal=request.signal;return new Promise((resolve,reject)=>signal.addEventListener('abort',()=>reject(new Error('fixture stalled read'))));}}});
   const poll=[...clock.pending.entries()].find(([id,timer])=>timer.ms===1500);assert(poll);clock.fire(poll[0]);await settle();const read=[...clock.pending.entries()].find(([id,timer])=>timer.ms===15000);assert(read);clock.fire(read[0]);await settle();assert(signal.aborted);assert(clock.cleared.includes(read[0]));assert([...clock.pending.values()].some(timer=>timer.ms===3000));visibility(f,'hidden');assert.equal(clock.pending.size,0);assert(!f.calls.some(c=>c.method==='POST'&&/text-generation|rescue/.test(c.url)));
  }finally{if(f)f.finish();clock.restore();}
 });
 await test('hidden_works_pauses_existing_timer_and_visible_restores_fresh_get',async()=>{
  const f=await fixture({route:'works',handler:(request,store)=>{if(request.url==='/api/auth/site/session')store.jobs[0]={id:'job',status:'processing',stage:'enhance',created_at:1};}});visibility(f,'hidden');await settle();const before=f.calls.length;await new Promise(resolve=>setTimeout(resolve,1600));assert.equal(f.calls.length,before);f.store.jobs[0]={...f.store.jobs[0],status:'succeeded',result_url:signed('done.jpg'),thumb_url:signed('thumb.jpg')};visibility(f,'visible');await settle();assert(f.calls.some(c=>c.url.includes('status=processing')));assert(f.doc.body.textContent.includes('已完成'));f.finish();
 });
 await test('work_poll_hard_deadline_does_not_run_after_thirty_minutes',async()=>{
  const f=await fixture({route:'works',handler:(request,store)=>{if(request.url==='/api/auth/site/session')store.jobs[0]={id:'job',status:'processing',stage:'enhance'};}});visibility(f,'hidden');await settle();const before=f.calls.length,original=Date.now;Date.now=()=>original()+31*60*1000;
  try{visibility(f,'visible');await settle();assert.equal(f.calls.length,before);}finally{Date.now=original;f.finish();}
 });
 await test('reselecting_same_community_filter_does_not_repeat_get',async()=>{
  const f=await fixture(),before=f.calls.length;click(f,'.lib-filter-active');await settle();assert.equal(f.calls.length,before);f.finish();
 });
 await test('hidden_creation_close_cancels_wait_without_any_network_or_resubmit',async()=>{
  const dom=new JSDOM('<main></main>',{pretendToBeVisual:true});let requests=0;const ctx={accountVersion:1,state:{user:null,config},document:dom.window.document,api:async()=>{requests++;return {status:'processing'};}};Object.defineProperty(ctx.document,'visibilityState',{configurable:true,value:'hidden'});
  const s=new CreationSession(ctx,{receipts:{},drafts:{}}),pending=s.poll('job',()=>{});s.close();await assert.rejects(pending,error=>error.code==='INACTIVE');assert.equal(requests,0);dom.window.close();
 });
 await test('template_search_preserves_same_card_and_uses_thumbnail_not_full_cover',async()=>{
  const f=await fixture({route:'templates',withCreation:true,session:null}),card=f.doc.querySelector('.creation-template-card'),img=card.querySelector('img');assert.equal(img.src,f.store.templates.items[0].thumbnail);const input=f.doc.querySelector('[aria-label="搜索风格"]');input.value='模板';input.dispatchEvent(new f.dom.window.Event('input',{bubbles:true}));assert.strictEqual(f.doc.querySelector('.creation-template-card'),card);assert.strictEqual(card.querySelector('img'),img);f.finish();
 });
 await test('failed_thumbnail_metadata_read_is_visible_and_not_false_full_success',async()=>{
  const f=await fixture({route:'works',handler:(request,store,response)=>request.url==='/api/jobs/job'?response({detail:'元数据网络失败'},503):undefined}),img=f.doc.querySelector('.work-card img'),before=img.src;img.dispatchEvent(new f.dom.window.Event('error'));await settle();assert.equal(img.src,before);assert(f.doc.body.textContent.includes('元数据网络失败'));assert(img.classList.contains('lib-image-error'));f.finish();
 });
 await test('financial_order_reads_never_use_display_cache',async()=>{
  const f=await fixture();const before=f.calls.length;await f.app.ctx.displayGet('/api/payment/orders?limit=30&offset=0');await f.app.ctx.displayGet('/api/payment/orders?limit=30&offset=0');await f.app.ctx.displayGet('/api/me/credits?limit=30&offset=0');await f.app.ctx.displayGet('/api/me/credits?limit=30&offset=0');assert.equal(f.calls.length-before,4);f.finish();
 });
 fs.mkdirSync(out,{recursive:true});fs.writeFileSync(path.join(out,'load_web_results.json'),JSON.stringify({summary:{total:cases.length,passed:cases.filter(row=>row.passed).length,failed:cases.filter(row=>!row.passed).length},measurements,cases},null,2),'utf8');
 const passed=cases.filter(row=>row.passed).length;console.log(`WEB_LOAD_SUMMARY total=${cases.length} passed=${passed} failed=${cases.length-passed}`);process.exitCode=passed===cases.length?0:1;
})().catch(error=>{console.error(error);process.exitCode=1;});
