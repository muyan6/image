/* Real jsdom event/tree tests; API, file-download and dialog platform boundaries are mocked. */
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const root=path.resolve(process.env.REVIEW_ROOT||path.join(__dirname,'../..'));
const out=path.resolve(process.env.REVIEW_OUTPUT||path.join(root,'audit/web-library-parity'));
const {JSDOM,VirtualConsole}=process.env.WEB_DOM_MODULE?require(process.env.WEB_DOM_MODULE):require('jsdom');
const cases=[];fs.mkdirSync(out,{recursive:true});
async function test(name,fn){try{await fn();cases.push({case:name,passed:true});console.log('PASS '+name);}catch(e){cases.push({case:name,passed:false,error:e.stack});console.error('FAIL '+name+': '+e.message);}}
(async()=>{
 const errors=[],virtualConsole=new VirtualConsole();virtualConsole.on('jsdomError',e=>errors.push(e.message));
 const dom=new JSDOM('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>'+fs.readFileSync(path.join(root,'backend/static/web/library.css'),'utf8')+'</style><main id="root"></main>',{url:'http://fixture.local/',runScripts:'outside-only',pretendToBeVisual:true,virtualConsole});
 const window=dom.window;window.structuredClone=value=>JSON.parse(JSON.stringify(value));window.downloads=[];
 window.HTMLDialogElement.prototype.showModal=function(){this.setAttribute('open','');};window.HTMLDialogElement.prototype.close=function(){this.removeAttribute('open');};
 window.HTMLAnchorElement.prototype.click=function(){window.downloads.push({filename:this.download,url:this.href});};
 window.URL.createObjectURL=()=> 'blob:http://fixture.local/mock-image';window.URL.revokeObjectURL=()=>{};
 window.fetch=async url=>{if(window.events)window.events.mediaFetches.push(url);return {ok:true,status:200,blob:async()=>new window.Blob(['fixture-image'],{type:'image/png'})};};
 const sharedSource=fs.readFileSync(path.join(root,'backend/static/web/shared-load.js'),'utf8').replace(/\bexport (?=(?:async )?function|class)/g,'');
 vm.runInContext(sharedSource+'\n'+fs.readFileSync(path.join(root,'backend/static/web/library.js'),'utf8').replace(/^import .*;\r?\n/gm,'').replace('export async function mountLibrary','async function mountLibrary')+'\nwindow.mountLibrary=mountLibrary;',dom.getInternalVMContext());
 const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
 function locator(select){
  const nodes=()=>typeof select==='function'?select():Array.from(window.document.querySelectorAll(select));
  const first=()=>{const n=nodes()[0];assert(n,'DOM control not found: '+select);return n;};
  return {first:()=>locator(()=>nodes().slice(0,1)),count:async()=>nodes().length,innerText:async()=>first().textContent,getAttribute:async name=>first().getAttribute(name),inputValue:async()=>first().value,
   isChecked:async()=>!!first().checked,click:async()=>{first().click();await delay(0);},fill:async text=>{const n=first();n.value=n.maxLength>0?text.slice(0,n.maxLength):text;n.dispatchEvent(new window.Event('input',{bubbles:true}));await delay(0);},
   check:async()=>{const n=first();n.checked=true;n.dispatchEvent(new window.Event('change',{bubbles:true}));await delay(0);}};
 }
 const page={locator,getByRole:(role,{name,exact=true})=>locator(()=>Array.from(window.document.querySelectorAll(role==='button'?'button':'[role="'+role+'"]')).filter(n=>exact?n.textContent===name:n.textContent.includes(name))),
  getByLabel:label=>locator(()=>Array.from(window.document.querySelectorAll('[aria-label]')).filter(n=>n.getAttribute('aria-label')===label)),waitForTimeout:delay,
  evaluate:async(fn,arg)=>vm.runInContext('('+fn.toString()+')('+JSON.stringify(arg)+')',dom.getInternalVMContext()),
  waitForEvent:async event=>{assert.equal(event,'download');const start=window.downloads.length;for(let i=0;i<100&&!window.downloads[start];i++)await delay(5);assert(window.downloads[start],'Download anchor was not invoked');return {suggestedFilename:()=>window.downloads[start].filename};}};
 await page.evaluate(async()=>{
  const {mountLibrary}=window;const clone=x=>structuredClone(x);
  window.setup=async(route,options={})=>{
   if(window.dispose)window.dispose();const root=document.querySelector('#root');root.dataset.state='';
   if(!window.nativeLibraryTimers)window.nativeLibraryTimers={set:window.setTimeout.bind(window),clear:window.clearTimeout.bind(window)};
   window.libraryTimers=[];window.setTimeout=options.fakeTimers?fn=>{const id=window.libraryTimers.length+1;window.libraryTimers.push({id,fn});return id;}:window.nativeLibraryTimers.set;
   window.clearTimeout=options.fakeTimers?id=>{window.libraryTimers=window.libraryTimers.filter(t=>t.id!==id);}:window.nativeLibraryTimers.clear;
   const now=Date.now()/1000;window.events={requests:[],navigations:[],toasts:[],logins:0,links:0,logouts:0,passwords:0,balances:[],mediaFetches:[]};
   window.store={jobs:[{id:'job0001',status:'succeeded',template_name:'复古胶片',created_at:now-1000,completed_at:now-900,expires_at:now+86400,result_url:'/fixture.png',orig_url:'/fixture.png',charged_amount:40,refunded_amount:0},
      {id:'job0002',status:'failed',error:'云端技术失败',created_at:now-2000,charged_amount:40,refunded_amount:40}],
    posts:[{id:'post001',title:'胶片色彩',story:'旧照片的新故事',authorName:'创作者',category:'film',categoryName:'复古胶片',templateId:'tpl1',templateName:'胶片',resultUrl:'/fixture.png',origUrl:'/fixture.png',likes:2,liked:false,date:'2026.10.02',comments:1},
      {id:'post002',title:'动漫重绘',story:'另一张作品',authorName:'画家',category:'anime',categoryName:'动漫重绘',resultUrl:'/fixture.png',origUrl:'',likes:0,liked:true,date:'2026.10.02',comments:0}],
    comments:[{id:'comment01',post_id:'post001',content:'喜欢这个效果',author_name:'我',created_at:now,mine:true,likes:0,liked:false}],
    submissions:[{id:'sub0001',job_id:'job0001',revision:1,status:'pending',title:'我的投稿',story:'故事',category:'film',share_original:false,submitted_at:now,reward:0,rewarded:false}],
    credits:[{id:'credit01',kind:'generation',title:'生成扣款',amount:-40,created_at:now,job_id:'job0001'},
      {id:'credit02',kind:'refund',title:'失败退回',amount:40,created_at:now,job_id:'job0002'}],
    orders:[{id:'order0001',status:'delivered',points:200,amount:600,created_at:now,refunded_fen:0,reversed_points:0}],
    invites:[{id:'invite01',nickname:'老朋友',bound_at:now,reward:40}],
    me:{user_id:'ACCOUNT_SHARED',username:'web_user',nickname:'创作者',account_type:'site',balance:0,wechat_bound:false},
    recipes:{job0001:{complete:true,recipe_complete:true,input_mode:'photo',orig_url:'https://cos.invalid/original.jpg',quality:'fine',aspect_ratio:'3:4',template_id:'tpl1',text_fields:{title:'旧照片'},custom_prompt:'',template_output_mode:'template'}},
    failRoutes:[],failCommentPosts:0,...clone(options.store||{})};
   const store=window.store;
   const slice=(items,u)=>{const offset=Number(u.searchParams.get('offset')||0),limit=Number(u.searchParams.get('limit')||24),rows=items.slice(offset,offset+limit);return {items:clone(rows),total:items.length,next_offset:offset+rows.length,has_more:offset+rows.length<items.length};};
   const api=async(raw,settings={})=>{
    const method=settings.method||'GET',data=settings.data||{},u=new URL(raw,location.origin),pathname=u.pathname;window.events.requests.push({path:raw,method,data:clone(data)});
    if(store.deferRoute&&raw.includes(store.deferRoute)){return await new Promise(r=>window.releaseDeferred=r);}
    if(store.failRoutes.some(p=>raw.includes(p)))throw new Error('fixture offline');
    if(pathname==='/api/me')return clone(store.me);
    if(pathname==='/api/my/jobs'&&method==='GET'){
      const status=u.searchParams.get('status')||'all',jobs=status==='all'?store.jobs:store.jobs.filter(j=>j.status===status),d=slice(jobs,u);d.jobs=d.items;delete d.items;d.processing_count=store.jobs.filter(j=>j.status==='processing').length;
      d.status_counts={all:store.jobs.length,processing:0,succeeded:0,failed:0};store.jobs.forEach(j=>d.status_counts[j.status]++);return d;
    }
    if(pathname==='/api/my/jobs'&&method==='DELETE'){store.jobs=[];return {ok:true};}
    if(pathname.startsWith('/api/my/jobs/')&&method==='DELETE'){store.jobs=store.jobs.filter(j=>j.id!==pathname.split('/').pop());return {ok:true};}
    if(pathname.startsWith('/api/jobs/')){const id=pathname.split('/')[3];if(pathname.endsWith('/refresh-media'))return {url:'https://cos.invalid/'+id+'_'+(u.searchParams.get('kind')||'result')+'.jpg'};if(pathname.endsWith('/recipe'))return clone(store.recipes[id]||{complete:false,missing_fields:['orig_url']});const row=store.jobs.find(j=>j.id===id);if(!row)throw Error('作品不存在或已过期');return clone(row);}
    if(pathname==='/api/community'){
      let rows=store.posts;const category=u.searchParams.get('category');if(category&&category!=='all')rows=rows.filter(p=>p.category===category);if(u.searchParams.get('liked_only')==='true')rows=rows.filter(p=>p.liked);return {enabled:true,...slice(rows,u)};
    }
    if(pathname==='/api/community/submissions/mine')return slice(store.submissions,u);
    if(pathname==='/api/community/submissions'&&method==='POST'){
      if(!data.consent)throw Error('缺少公开授权');const existing=store.submissions.find(s=>s.job_id===data.job_id);if(existing)Object.assign(existing,data,{status:'pending'});else store.submissions.push({...data,id:'sub_new',status:'pending',revision:1});return {submission:clone(existing||store.submissions.at(-1))};
    }
    if(pathname.includes('/submissions/')&&pathname.endsWith('/withdraw')){const id=pathname.split('/')[4],row=store.submissions.find(s=>s.id===id);if(row.revision!==data.revision)throw Error('投稿版本已更新');row.status='withdrawn';return {ok:true,submission:clone(row)};}
    if(pathname.startsWith('/api/community/posts/')){
      const id=pathname.split('/')[4],post=store.posts.find(p=>p.id===id);if(!post){const error=Error('帖子已下架或不存在');error.status=404;throw error;}
      if(pathname.endsWith('/like')){post.likes+=data.liked===post.liked?0:data.liked?1:-1;post.liked=data.liked;return {id,likes:post.likes,liked:post.liked};}
      if(pathname.endsWith('/report'))return {ok:true};
      if(pathname.endsWith('/comments')&&method==='GET')return slice(store.comments.filter(c=>c.post_id===id),u);
      if(pathname.endsWith('/comments')&&method==='POST'){
        let row=store.comments.find(c=>c.request_id===data.request_id);if(!row){row={id:'comment'+(store.comments.length+1),post_id:id,content:data.content,request_id:data.request_id,mine:true,author_name:'我',likes:0,liked:false,created_at:now};store.comments.unshift(row);}
        if(store.failCommentPosts-->0)throw Error('响应丢失，请重试');return {ok:true,id:row.id};
      }
      return {post:clone(post)};
    }
    if(pathname.startsWith('/api/community/comments/')){const id=pathname.split('/')[4],row=store.comments.find(c=>c.id===id);
      if(method==='DELETE'){store.comments=store.comments.filter(c=>c.id!==id);return {ok:true};}
      if(pathname.endsWith('/like')){row.liked=data.liked;row.likes=data.liked?1:0;return clone(row);}
      if(pathname.endsWith('/report'))return {ok:true};
    }
    if(pathname==='/api/me/profile'){store.me.nickname=data.nickname;return {nickname:data.nickname};}
    if(pathname==='/api/me/violation-feedback'){
      const job=store.jobs.find(j=>j.violation&&j.violation.violation_id===data.violation_id);if(!job||job.violation.feedback_submitted)throw Error('违规记录不存在或已经反馈');job.violation.feedback_submitted=true;return {ok:true,message:'反馈已提交，等待人工复核'};
    }
    if(pathname==='/api/me/credits')return slice(store.credits,u);
    if(pathname==='/api/payment/orders')return {...slice(store.orders,u),balance:store.me.balance};
    if(pathname.startsWith('/api/payment/orders/'))return {order:clone(store.orders.find(o=>o.id===pathname.split('/')[4])),balance:store.me.balance};
    if(pathname==='/api/me/invites')return {...slice(store.invites,u),recorded_reward:store.invites.reduce((n,r)=>n+(r.reward||0),0),historical_unknown:store.invites.filter(r=>r.reward==null).length};
    throw Error('Unexpected fixture API '+method+' '+raw);
   };
   window.ctx={api,accountVersion:1,state:{user:clone(store.me),config:{},auth:{auth_source:'site',manual_credit_only:true,wechat_login:{ready:!!options.bindingReady}}},
    requireLogin:async()=>{window.events.logins++;return options.loggedIn!==false;},navigate:(route,params)=>window.events.navigations.push({route,params}),toast:message=>window.events.toasts.push(message),
    element:(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!=null)n.textContent=String(text);return n;},confirm:async()=>options.confirm!==false,
    setBalance:n=>window.events.balances.push(n),startWechatLink:async()=>{window.events.links++;if(!options.bindingReady)throw Error('微信账号接入尚未配置，配置完成后可在这里绑定');},
    changePassword:()=>window.events.passwords++,logout:()=>{window.events.logouts++;window.ctx.accountVersion++;}};
   window.dispose=await mountLibrary(window.ctx,root,route,options.params||{});
  };
 });
 const setup=async(route,options={})=>{await page.evaluate(([r,o])=>window.setup(r,o),[route,options]);await page.waitForTimeout(30);};
 const events=()=>page.evaluate(()=>window.events);
 await test('anonymous_private_routes_require_registration_without_user_api_requests',async()=>{
  await setup('works',{loggedIn:false});assert((await page.locator('#root').innerText()).includes('注册网页账号'));assert.equal((await events()).requests.length,0);
 });
 await test('public_community_is_readable_without_login_and_likes_require_login',async()=>{
  await setup('community',{loggedIn:false});assert.equal(await page.locator('.community-card').count(),2);assert.equal((await events()).logins,0);
  await page.locator('[data-action="like-post"]').first().click();assert.equal((await events()).logins,1);assert(!(await events()).requests.some(r=>r.method==='PUT'));
 });
 await test('works_first_page_is_bounded_status_filters_and_load_more_are_real_dom',async()=>{
  const jobs=Array.from({length:26},(_,i)=>({id:'job'+i,status:i===25?'failed':'succeeded',result_url:'/fixture.png',created_at:1}));await setup('works',{store:{jobs}});
  assert.equal(await page.locator('.work-card').count(),24);await page.locator('[data-action="load-more"]').click();assert.equal(await page.locator('.work-card').count(),26);
  await page.locator('[data-action="status-failed"]').click();await page.waitForTimeout(20);assert.equal(await page.locator('.work-card').count(),1);assert((await events()).requests.at(-1).path.includes('status=failed'));
 });
 await test('works_expiry_and_exact_refund_are_visible_without_claiming_unknown_refunds',async()=>{
  await setup('works');const text=await page.locator('#root').innerText();assert(text.includes('还剩 1 天'));assert(text.includes('扣除 40 · 已退回 40 光子'));
 });
 await test('violation_penalty_is_displayed_separately_from_generation_refund',async()=>{
  await setup('works',{store:{jobs:[{id:'job0001',status:'failed',charged_amount:40,refunded_amount:40,violation:{violation_id:'violation123',message:'服务器审核原因',charged:60,weekly_count:3,banned:true,feedback_submitted:false}}]}});const text=await page.locator('#root').innerText();assert(text.includes('扣除 40 · 已退回 40 光子'));assert(text.includes('内容审核单独扣除 60 光子'));assert(text.includes('近 7 天累计 3 次'));assert(text.includes('已封禁'));assert(text.includes('服务器审核原因'));
 });
 await test('violation_feedback_submits_exact_id_message_and_marks_only_after_success',async()=>{
  await setup('works',{store:{jobs:[{id:'job0001',status:'failed',violation:{violation_id:'violation123',charged:60,weekly_count:3,banned:true,feedback_submitted:false}}]}});await page.locator('[data-action="violation-feedback"]').click();await page.getByLabel('误判反馈内容').fill('请复核这次审核');await page.locator('[data-action="send-violation-feedback"]').click();await page.waitForTimeout(20);const call=(await events()).requests.find(r=>r.path==='/api/me/violation-feedback');assert.deepEqual(call.data,{violation_id:'violation123',message:'请复核这次审核'});assert(window.document.querySelector('[data-action="violation-feedback"]').disabled);assert(window.store.jobs[0].violation.banned);assert.equal(window.store.jobs[0].violation.charged,60);
 });
 await test('violation_feedback_already_submitted_and_inflight_duplicate_are_blocked',async()=>{
  await setup('works',{store:{jobs:[{id:'job0001',status:'failed',violation:{violation_id:'violation123',feedback_submitted:true}}]}});assert(window.document.querySelector('[data-action="violation-feedback"]').disabled);await page.locator('[data-action="violation-feedback"]').click();assert.equal(await page.locator('dialog').count(),0);
  await setup('works',{store:{deferRoute:'/api/me/violation-feedback',jobs:[{id:'job0001',status:'failed',violation:{violation_id:'violation123',feedback_submitted:false}}]}});await page.locator('[data-action="violation-feedback"]').click();await page.getByLabel('误判反馈内容').fill('申请复核');await page.locator('[data-action="send-violation-feedback"]').click();await page.getByRole('button',{name:'关闭',exact:true}).click();await page.locator('[data-action="violation-feedback"]').click();assert.equal(await page.locator('dialog').count(),0);assert.equal((await events()).requests.filter(r=>r.path==='/api/me/violation-feedback').length,1);await page.evaluate(()=>window.releaseDeferred({ok:true}));await page.waitForTimeout(20);
 });
 await test('violation_feedback_failure_preserves_text_and_does_not_mark_or_adjust_account',async()=>{
  await setup('works',{store:{failRoutes:['/api/me/violation-feedback'],jobs:[{id:'job0001',status:'failed',violation:{violation_id:'violation123',charged:60,banned:true,feedback_submitted:false}}]}});await page.locator('[data-action="violation-feedback"]').click();await page.getByLabel('误判反馈内容').fill('保留这段复核描述');await page.locator('[data-action="send-violation-feedback"]').click();await page.waitForTimeout(20);assert.equal(await page.getByLabel('误判反馈内容').inputValue(),'保留这段复核描述');assert(!window.store.jobs[0].violation.feedback_submitted);assert(window.store.jobs[0].violation.banned);assert.equal((await events()).balances.length,0);await page.evaluate(()=>window.store.failRoutes=[]);await page.locator('[data-action="send-violation-feedback"]').click();await page.waitForTimeout(20);assert(window.store.jobs[0].violation.feedback_submitted);
 });
 await test('work_poll_checks_processing_only_and_reconciles_completed_loaded_job',async()=>{
  await setup('works',{fakeTimers:true,store:{jobs:[{id:'pending1',status:'processing',stage:'queued',created_at:1,result_url:''}]}});
  assert((await page.locator('#root').innerText()).includes('任务排队中'));assert.equal(window.libraryTimers.length,1);
  await page.evaluate(()=>{window.store.jobs[0]={...window.store.jobs[0],status:'succeeded',result_url:'/fixture.png'};window.libraryTimers.shift().fn();});await page.waitForTimeout(20);
  assert((await page.locator('#root').innerText()).includes('已完成'));const calls=(await events()).requests;assert(calls.some(r=>r.path.endsWith('status=processing')));assert(calls.some(r=>r.path==='/api/jobs/pending1'));assert.equal(calls.filter(r=>r.path.endsWith('status=all')).length,1);
 });
 await test('delete_single_work_uses_owner_endpoint_and_preserves_other_rows',async()=>{
  await setup('works');await page.locator('[data-id="job0001"] [data-action="delete-work"]').click();await page.waitForTimeout(20);assert.equal(await page.locator('.work-card').count(),1);
  assert((await events()).requests.some(r=>r.path==='/api/my/jobs/job0001'&&r.method==='DELETE'));
 });
 await test('canceling_delete_does_not_mutate_work_or_call_delete_endpoint',async()=>{
  await setup('works',{confirm:false});await page.locator('[data-action="delete-work"]').first().click();assert.equal(await page.locator('.work-card').count(),2);assert(!(await events()).requests.some(r=>r.method==='DELETE'));
 });
 await test('clear_all_work_requires_confirmation_and_server_delete',async()=>{
  await setup('works');await page.getByRole('button',{name:'清空个人作品',exact:true}).click();await page.waitForTimeout(20);assert.equal(await page.locator('.work-card').count(),0);assert((await events()).requests.some(r=>r.path==='/api/my/jobs'&&r.method==='DELETE'));
 });
 await test('comparison_opens_real_accessible_dialog_with_both_images',async()=>{
  await setup('works');await page.getByRole('button',{name:'查看对比',exact:true}).click();await page.waitForTimeout(20);assert.equal(await page.locator('dialog[open]').count(),1);assert.equal(await page.locator('dialog img').count(),2);await page.getByRole('button',{name:'关闭',exact:true}).click();assert.equal(await page.locator('dialog').count(),0);
 });
 await test('download_refreshes_signed_media_and_invokes_owned_download_anchor',async()=>{
  await setup('works');const download=page.waitForEvent('download');await page.locator('[data-action="download"]').first().click();const saved=await download;assert(saved.suggestedFilename().includes('job0001'));assert((await events()).requests.some(r=>r.path==='/api/jobs/job0001'));
 });
 await test('personal_old_same_origin_images_are_not_loaded_as_thumbnail_or_byte_proxy',async()=>{
  await setup('works',{store:{jobs:[{id:'job0001',status:'succeeded',result_url:'/api/images/legacy.jpg',orig_url:'/api/images/orig.jpg'}]}});assert.equal(await page.locator('.work-card img').count(),0);assert.equal((await events()).mediaFetches.length,0);
 });
 await test('personal_legacy_download_repairs_json_then_fetches_only_cos_bytes',async()=>{
  await setup('works',{store:{jobs:[{id:'job0001',status:'succeeded',result_url:'/api/images/legacy.jpg'}]}});await page.locator('[data-action="download"]').click();await page.waitForTimeout(20);
  const e=await events();assert(e.requests.some(r=>r.path==='/api/jobs/job0001/refresh-media?kind=result'&&r.method==='POST'));assert.equal(e.mediaFetches.length,1);assert(e.mediaFetches[0].startsWith('https://cos.invalid/'));assert(!e.mediaFetches.some(u=>u.includes('/api/images/')));
 });
 await test('personal_media_repair_failure_never_fetches_proxy_or_claims_saved_image',async()=>{
  await setup('works',{store:{jobs:[{id:'job0001',status:'succeeded',result_url:'/api/images/legacy.jpg'}],failRoutes:['/refresh-media']}});await page.locator('[data-action="download"]').click();await page.waitForTimeout(20);const e=await events();assert.equal(e.mediaFetches.length,0);assert(!e.toasts.some(t=>t.includes('照片已下载')));
 });
 await test('recreate_preserves_complete_recipe_and_blocks_incomplete_legacy_recipe',async()=>{
  await setup('works');await page.getByRole('button',{name:'沿用参数再创作',exact:true}).click();await page.waitForTimeout(20);let e=await events();assert.equal(e.navigations[0].route,'create');assert.equal(e.navigations[0].params.recipe.aspect_ratio,'3:4');
  await setup('works',{store:{recipes:{job0001:{complete:false}}}});await page.getByRole('button',{name:'沿用参数再创作',exact:true}).click();assert.equal((await events()).navigations.length,0);assert((await events()).toasts[0].includes('参数不完整'));
 });
 await test('text_recipe_navigates_to_text_without_fabricating_an_original_image',async()=>{
  await setup('works',{store:{recipes:{job0001:{complete:true,input_mode:'text',prompt:'旧式照相馆',aspect_ratio:'1:1'}}}});await page.getByRole('button',{name:'沿用参数再创作',exact:true}).click();assert.equal((await events()).navigations[0].route,'text');
 });
 await test('photo_recreate_rejects_missing_or_unsafe_original_without_navigation',async()=>{
  await setup('works',{store:{recipes:{job0001:{complete:true,input_mode:'photo',orig_url:'javascript:alert(1)'}}}});await page.getByRole('button',{name:'沿用参数再创作',exact:true}).click();assert.equal((await events()).navigations.length,0);assert((await events()).toasts[0].includes('原始照片'));
 });
 await test('community_category_and_liked_filters_call_exact_paged_api',async()=>{
  await setup('community');await page.getByRole('button',{name:'复古胶片',exact:true}).click();await page.waitForTimeout(20);assert.equal(await page.locator('.community-card').count(),1);
  await page.locator('[data-action="liked-filter"]').click();await page.waitForTimeout(20);assert.equal(await page.locator('.community-card').count(),1);assert.equal(await page.locator('.community-card').getAttribute('data-id'),'post002');assert((await events()).requests.at(-1).path.includes('liked_only=true'));
 });
 await test('community_like_result_is_server_authoritative_and_renders_updated_count',async()=>{
  await setup('community');await page.locator('[data-id="post001"] [data-action="like-post"]').click();assert((await page.locator('[data-id="post001"]').innerText()).includes('♥ 已喜欢 3'));
 });
 await test('category_change_invalidates_an_older_feed_response',async()=>{
  await setup('community',{store:{deferRoute:'category=all'}});await page.getByRole('button',{name:'复古胶片',exact:true}).click();await page.waitForTimeout(20);
  await page.evaluate(()=>window.releaseDeferred({items:[{id:'old-public',title:'旧快照',category:'anime',resultUrl:'/fixture.png'}],total:1}));await page.waitForTimeout(20);
  assert.equal(await page.locator('.community-card').getAttribute('data-id'),'post001');assert(!(await page.locator('#root').innerText()).includes('旧快照'));
 });
 await test('post_reads_public_detail_and_comment_list_without_auth_side_effects',async()=>{
  await setup('post',{loggedIn:false,params:{id:'post001'}});assert.equal(await page.locator('.lib-comment').count(),1);assert.equal((await events()).logins,0);assert((await page.locator('#root').innerText()).includes('喜欢这个效果'));
 });
 await test('comment_create_retries_with_same_receipt_after_lost_response',async()=>{
  await setup('post',{params:{id:'post001'},store:{failCommentPosts:1}});await page.getByLabel('作品留言').fill('新的灵感');await page.getByRole('button',{name:'发布留言',exact:true}).click();await page.waitForTimeout(20);assert.equal(await page.getByLabel('作品留言').inputValue(),'新的灵感');
  await page.getByRole('button',{name:'发布留言',exact:true}).click();await page.waitForTimeout(20);const writes=(await events()).requests.filter(r=>r.method==='POST'&&r.path.endsWith('/comments'));
  assert.equal(writes.length,2);assert.equal(writes[0].data.request_id,writes[1].data.request_id);assert.equal(await page.locator('.lib-comment').count(),2);assert.equal(await page.getByLabel('作品留言').inputValue(),'');
 });
 await test('comment_delete_and_like_use_real_dom_controls_and_owner_actions',async()=>{
  await setup('post',{params:{id:'post001'}});await page.locator('.lib-comment .lib-link').first().click();assert((await events()).requests.some(r=>r.path==='/api/community/comments/comment01/like'&&r.method==='PUT'));
  await page.locator('[data-action="delete-comment"]').click();await page.waitForTimeout(20);assert.equal(await page.locator('.lib-comment').count(),0);
 });
 await test('comment_copy_is_not_falsely_labeled_server_edit',async()=>{
  await setup('post',{params:{id:'post001'}});await page.getByRole('button',{name:'复制到留言框',exact:true}).click();assert.equal(await page.getByLabel('作品留言').inputValue(),'喜欢这个效果');assert(!(await events()).requests.some(r=>r.method==='PUT'&&!r.path.endsWith('/like')));
 });
 await test('comment_moderation_failure_keeps_draft_and_never_claims_publication',async()=>{
  await setup('post',{params:{id:'post001'}});await page.evaluate(()=>window.store.failRoutes=['/comments']);await page.getByLabel('作品留言').fill('待审核的留言');await page.getByRole('button',{name:'发布留言',exact:true}).click();await page.waitForTimeout(20);
  assert.equal(await page.getByLabel('作品留言').inputValue(),'待审核的留言');assert(!(await events()).toasts.some(t=>t.includes('并发布')));
 });
 await test('report_modal_submits_chosen_reason_not_unreviewed_content',async()=>{
  await setup('post',{params:{id:'post001'}});await page.getByRole('button',{name:'举报作品',exact:true}).click();await page.getByRole('button',{name:'侵犯隐私',exact:true}).click();await page.waitForTimeout(20);const report=(await events()).requests.find(r=>r.path.endsWith('/report'));assert.equal(report.data.reason,'privacy');assert.equal(await page.locator('dialog').count(),0);
 });
 await test('submission_form_defaults_private_original_and_requires_fresh_consent',async()=>{
  await setup('submit',{params:{jobId:'job0001'}});assert(!await page.locator('[data-action="share-original"]').isChecked());assert(!await page.locator('[data-action="consent"]').isChecked());
  await page.locator('[data-action="submit-post"]').click();assert(!(await events()).requests.some(r=>r.path==='/api/community/submissions'&&r.method==='POST'));assert((await events()).toasts[0].includes('授权'));
  await page.locator('[data-action="consent"]').check();await page.locator('[data-action="share-original"]').check();assert(!await page.locator('[data-action="consent"]').isChecked());assert((await page.locator('.lib-consent-summary').innerText()).includes('成品 + 修护前原图'));
 });
 await test('submission_posts_explicit_authorization_and_preserves_exact_selected_media',async()=>{
  await setup('submit',{params:{jobId:'job0001'}});await page.getByLabel('作品标题').fill('重新展览');await page.getByLabel('创作故事').fill('我决定公开这张照片');await page.locator('[data-action="consent"]').check();await page.locator('[data-action="submit-post"]').click();await page.waitForTimeout(20);
  const write=(await events()).requests.find(r=>r.path==='/api/community/submissions'&&r.method==='POST');assert.equal(write.data.share_original,false);assert.equal(write.data.consent,true);assert.equal(write.data.job_id,'job0001');assert.equal((await events()).navigations[0].route,'submissions');
 });
 await test('submission_keeps_authorized_fields_locked_until_server_result',async()=>{
  await setup('submit',{params:{jobId:'job0001'}});await page.evaluate(()=>window.store.deferRoute='/api/community/submissions');await page.locator('[data-action="consent"]').check();await page.locator('[data-action="submit-post"]').click();
  assert(window.document.querySelector('[aria-label="作品标题"]').disabled);assert(window.document.querySelector('[data-action="share-original"]').disabled);
  await page.evaluate(()=>window.releaseDeferred({submission:{status:'pending'}}));await page.waitForTimeout(20);assert(!window.document.querySelector('[aria-label="作品标题"]').disabled);
 });
 await test('my_submissions_show_status_withdraw_revision_and_retry_presets',async()=>{
  await setup('submissions');assert((await page.locator('#root').innerText()).includes('等待审核'));await page.locator('[data-action="withdraw"]').click();await page.waitForTimeout(20);assert((await events()).requests.some(r=>r.path==='/api/community/submissions/sub0001/withdraw'&&r.data.revision===1));
  await page.getByRole('button',{name:'修改后再投稿',exact:true}).click();assert.equal((await events()).navigations[0].params.jobId,'job0001');assert.equal((await events()).navigations[0].params.preset.title,'我的投稿');
 });
 await test('my_account_shows_registration_binding_and_manual_balance_only',async()=>{
  await setup('my');const text=await page.locator('#root').innerText();assert(text.includes('web_user'));assert(text.includes('0 光子'));assert(text.includes('后台手动'));assert(!/每日签到|看视频|选择套餐|购买光子/.test(text));
  await page.locator('[data-action="wechat-link"]').click();assert.equal((await events()).links,1);assert((await events()).toasts[0].includes('尚未配置'));
 });
 await test('profile_nickname_is_saved_via_owner_api_and_rendered_as_plain_text',async()=>{
  await setup('my');await page.getByLabel('展示昵称').fill('<img src=x onerror=alert(1)>');await page.getByRole('button',{name:'保存昵称',exact:true}).click();await page.waitForTimeout(20);assert.equal(await page.locator('.lib-card h2').innerText(),'<img src=x onerror=alert');assert.equal(await page.locator('.lib-card img').count(),0);
 });
 await test('late_profile_write_cannot_replace_a_new_account_nickname',async()=>{
  await setup('my');await page.evaluate(()=>window.store.deferRoute='/api/me/profile');await page.getByLabel('展示昵称').fill('旧请求昵称');await page.getByRole('button',{name:'保存昵称',exact:true}).click();
  await page.evaluate(()=>{window.ctx.accountVersion++;window.ctx.state.user.nickname='新账号昵称';window.releaseDeferred({nickname:'旧请求昵称'});});await page.waitForTimeout(20);assert.equal(window.ctx.state.user.nickname,'新账号昵称');assert(!(await events()).toasts.includes('昵称已保存'));
 });
 await test('bound_account_is_marked_shared_and_does_not_offer_duplicate_linking',async()=>{
  await setup('my',{bindingReady:true,store:{me:{user_id:'ACCOUNT_SHARED',username:'web_user',nickname:'绑定用户',balance:125,wechat_bound:true}}});assert((await page.locator('#root').innerText()).includes('作品和账务使用同一账号'));assert(window.document.querySelector('[data-action="wechat-link"]').disabled);assert((await page.locator('#root').innerText()).includes('✦ 125'));
 });
 await test('account_password_and_logout_delegate_to_root_session_controls',async()=>{
  await setup('my');await page.getByRole('button',{name:'修改密码',exact:true}).click();assert.equal((await events()).passwords,1);await page.getByRole('button',{name:'退出登录',exact:true}).click();assert.equal((await events()).logouts,1);
 });
 await test('credits_paginate_signed_amounts_and_link_shared_work_and_order_ids',async()=>{
  const credits=Array.from({length:31},(_,i)=>({id:'c'+i,title:'记录 '+i,amount:i===0?-40:10,created_at:1,...(i===0?{job_id:'job0001'}:i===1?{order_id:'order0001'}:{})}));await setup('credits',{store:{credits}});assert.equal(await page.locator('.lib-ledger-row').count(),30);assert((await page.locator('#root').innerText()).includes('-40 光子'));
  await page.getByRole('button',{name:'查看关联订单 ›',exact:true}).click();assert.equal((await events()).navigations[0].params.id,'order0001');await page.locator('[data-action="load-more"]').click();assert.equal(await page.locator('.lib-ledger-row').count(),31);
 });
 await test('orders_are_read_only_except_authoritative_sync_and_have_no_purchase_controls',async()=>{
  await setup('orders',{params:{id:'order0001'}});assert((await page.locator('#root').innerText()).includes('光子已到账'));await page.locator('[data-action="sync-order"]').click();await page.waitForTimeout(20);const writes=(await events()).requests.filter(r=>r.method!=='GET');assert.equal(writes.length,1);assert.equal(writes[0].path,'/api/payment/orders/order0001/sync');assert(!/立即购买|选择套餐|充值支付/.test(await page.locator('#root').innerText()));
 });
 await test('orders_page_can_read_history_beyond_fifty_with_paged_owner_queries',async()=>{
  const orders=Array.from({length:61},(_,i)=>({id:'order'+i,status:'delivered',points:40,amount:100,created_at:1,refunded_fen:0}));await setup('orders',{store:{orders}});assert.equal(await page.locator('.lib-order').count(),30);
  await page.locator('[data-action="load-more"]').click();await page.locator('[data-action="load-more"]').click();assert.equal(await page.locator('.lib-order').count(),61);assert((await events()).requests.at(-1).path.includes('offset=60'));
 });
 await test('invites_read_shared_history_without_binding_or_reward_post',async()=>{
  await setup('invites');assert((await page.locator('#root').innerText()).includes('老朋友'));assert((await page.locator('#root').innerText()).includes('+40 光子'));assert(!(await events()).requests.some(r=>r.method!=='GET'));
 });
 await test('read_failure_is_retryable_and_not_misrepresented_as_empty',async()=>{
  await setup('credits',{store:{failRoutes:['/api/me/credits']}});assert((await page.locator('#root').innerText()).includes('fixture offline'));assert(!(await page.locator('#root').innerText()).includes('暂无已记录'));
  await page.evaluate(()=>window.store.failRoutes=[]);await page.getByRole('button',{name:'重新加载',exact:true}).click();await page.waitForTimeout(20);assert.equal(await page.locator('.lib-ledger-row').count(),2);
 });
 await test('late_private_response_is_discarded_after_account_changes',async()=>{
  await setup('credits',{store:{deferRoute:'/api/me/credits'}});await page.evaluate(()=>{window.ctx.accountVersion++;window.releaseDeferred({items:[{id:'old-account',title:'旧账号金额',amount:999,created_at:1}],total:1});});await page.waitForTimeout(20);assert(!(await page.locator('#root').innerText()).includes('旧账号金额'));
 });
 await test('cleanup_discards_late_responses_and_removes_open_media_dialogs',async()=>{
  await setup('works');await page.getByRole('button',{name:'查看对比',exact:true}).click();assert.equal(await page.locator('dialog').count(),1);await page.evaluate(()=>window.dispose());assert.equal(await page.locator('dialog').count(),0);
  await setup('credits',{store:{deferRoute:'/api/me/credits'}});await page.evaluate(()=>{window.dispose();window.releaseDeferred({items:[{id:'old-view',title:'旧视图金额',amount:999,created_at:1}],total:1});});await page.waitForTimeout(20);assert(!(await page.locator('#root').innerText()).includes('旧视图金额'));
 });
 await test('all_routes_render_real_dom_and_dialog_close_remains_reachable',async()=>{
  for(const route of ['works','community','post','submissions','submit','my','credits','orders','invites']){
    await setup(route,{params:{id:route==='orders'?'order0001':'post001',jobId:'job0001'}});assert.equal(await page.locator('h1').count(),1,route+' heading');assert.equal(window.document.querySelector('#root').dataset.state,'loaded',route+' state');
  }
  await setup('works');await page.getByRole('button',{name:'查看对比',exact:true}).click();assert.equal(await page.locator('dialog[open]').count(),1);assert.equal(await page.getByRole('button',{name:'关闭',exact:true}).count(),1);await page.getByRole('button',{name:'关闭',exact:true}).click();
  fs.writeFileSync(path.join(out,'library_fixture_dom.html'),window.document.documentElement.outerHTML,'utf8');
 });
 await test('no_unhandled_browser_exceptions_or_unsafe_server_markup',async()=>{assert.deepEqual(errors,[]);});
 const failed=cases.filter(x=>!x.passed).length;fs.writeFileSync(path.join(out,'web_library_parity_results.json'),JSON.stringify({cases,total:cases.length,failed},null,2),'utf8');
 console.log(`WEB_LIBRARY_PARITY_SUMMARY total=${cases.length} passed=${cases.length-failed} failed=${failed}`);process.exitCode=failed?1:0;if(window.dispose)window.dispose();dom.window.close();
})().catch(e=>{console.error(e.stack);process.exitCode=1;});
