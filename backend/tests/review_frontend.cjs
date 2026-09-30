/* Offline execution of current mini-program and web code with wx/DOM doubles. */
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const cp = require('child_process');
const HERE = path.resolve(process.env.REVIEW_OUTPUT || __dirname);
fs.mkdirSync(HERE,{recursive:true});
const ROOT = path.resolve(process.env.REVIEW_ROOT || path.resolve(__dirname,'../..'));
const results = [];
const silent = {log(){}, warn(){}, error(){}};
const tick = () => new Promise(r => setImmediate(r));

async function test(name, fn) {
  try {
    const [passed, observed] = await fn();
    results.push({case:name, passed:!!passed, observed});
    console.log((passed?'PASS ':'FAIL ')+name+': '+JSON.stringify(observed));
  } catch(error) {
    results.push({case:name, passed:false, harness_error:String(error.stack)});
    console.log('ERROR '+name+': '+error.stack);
  }
}

function appFixture(history=[]) {
  return {globalData:{apiBase:'https://fixture.invalid',lightPoints:90,freeMode:false,historyList:history},
    setBalance(n){this.globalData.lightPoints=n;},persist(){},clearHistory(){this.globalData.historyList=[];}};
}

function apiModule(wx, app, clock={}) {
  const sandbox={module:{exports:{}},getApp:()=>app,wx,console:silent,
    setTimeout:clock.setTimeout||setTimeout,Date:clock.Date||Date};
  vm.runInNewContext(fs.readFileSync(path.join(ROOT,'miniprogram/utils/api.js'),'utf8'),sandbox);
  return sandbox.module.exports;
}

function loadPage(name, api={}, app=appFixture(), extraWx={}, clock={}) {
  let page;
  const wx=Object.assign({showToast(){},showModal(o){o.success?.({confirm:true});},
    showActionSheet(){},showLoading(){},hideLoading(){},vibrateShort(){},
    stopPullDownRefresh(){},getStorageSync(){return '';},setStorageSync(){},
    navigateTo(){},redirectTo(){},switchTab(){}},extraWx);
  vm.runInNewContext(fs.readFileSync(path.join(ROOT,`miniprogram/pages/${name}/${name}.js`),'utf8'),
    {getApp:()=>app,require:()=>api,Page:p=>page=p,wx,console:silent,
      setTimeout:clock.setTimeout||setTimeout,clearTimeout:clock.clearTimeout||clearTimeout,Date:clock.Date||Date});
  page.data=JSON.parse(JSON.stringify(page.data || {}));
  page.setData=function(data){Object.assign(this.data,data);};
  return {page,wx,app};
}

(async()=>{
  await test('template_popularity_orders_success_counts_without_breaking_search',()=>{
    const {page}=loadPage('templates',{absolute:x=>x});
    page._renderData([{id:'art',name:'艺术'}],[
      {id:'old',name:'水彩一',group_id:'art',usage_count:2},
      {id:'hot',name:'水彩二',group_id:'art',usage_count:9},
      {id:'tie',name:'胶片',group_id:'art',usage_count:9}]);
    const order=page.data.allTemplates.map(t=>t.id).join();
    page.onSearchInput({detail:{value:'水彩'}});
    return [order==='hot,tie,old'&&page.data.filteredTemplates.map(t=>t.id).join()==='hot,old',
      {order,search:page.data.filteredTemplates.map(t=>t.id)}];
  });
  await test('template_search_combines_category_and_keyword_and_clears',()=>{
    const {page}=loadPage('templates',{absolute:x=>x});
    page._renderData([{id:'art',name:'艺术'},{id:'portrait',name:'人像'}],
      [{id:'a',name:'水彩山川',subtitle:'旅行海报',group_id:'art'},
       {id:'b',name:'WATERCOLOR 肖像',group_id:'portrait'},
       {id:'c',name:'胶片',group_id:'art',tags:['复古']}]);
    if(!page.onSearchInput)return [false,{searchHandler:'missing'}];
    page.onSearchInput({detail:{value:'  WATERCOLOR  '}});
    const english=page.data.filteredTemplates.map(t=>t.id).join();
    page.onSearchInput({detail:{value:'复古'}});
    const tags=page.data.filteredTemplates.map(t=>t.id).join();
    page.onSelectCategory({currentTarget:{dataset:{id:'portrait'}}});
    const empty=page.data.filteredTemplates.length;
    page.onClearSearch();
    return [english==='b'&&tags==='c'&&empty===0&&page.data.filteredTemplates[0].id==='b',
      {english,tags,empty,afterClear:page.data.filteredTemplates.map(t=>t.id)}];
  });
  await test('optional_prompt_collapsed_and_disabled_draft_not_submitted',async()=>{
    let sent;
    const {page}=loadPage('adjust',{submitJob:async(p,f)=>{sent=f;throw new Error('stop before real upload');}});
    const xml=fs.readFileSync(path.join(ROOT,'miniprogram/pages/adjust/adjust.wxml'),'utf8');
    const collapsed=page.data.showCustomPrompt===false;
    page.setData({customPrompt:'remove everything',showCustomPrompt:false});
    await page.executeUpload('/isolated.jpg');
    return [collapsed&&!sent.custom_prompt&&xml.includes('可选')&&xml.includes('不用填写'),
      {collapsed,submittedPrompt:sent.custom_prompt||'',optionalLabel:xml.includes('可选')}];
  });
  await test('template_single_output_sets_mode_without_text_overlay_or_crop',async()=>{
    let sent;
    const {page}=loadPage('adjust',{submitJob:async(p,f)=>{sent=f;throw new Error('stop before real upload');}});
    if(!page.onSelectTemplateOutput)return [false,{outputHandler:'missing'}];
    page.setData({selectedTemplate:{id:'poster',engine:'fine',text_fields:[{key:'title'}]},singleOutputAvailable:true,
      textValues:{title:'template text'},currentRatioKey:'1:1'});
    page.onSelectTemplateOutput({currentTarget:{dataset:{mode:'single'}}});
    await page.executeUpload('/isolated.jpg');
    const single=sent.template_output_mode==='single'&&!sent.text_fields&&!sent.aspect_ratio;
    page.onSelectTemplate({currentTarget:{dataset:{template:{id:'next',text_fields:[]}}}});
    return [single&&page.data.templateOutputMode==='template',{single,modeAfterSwitch:page.data.templateOutputMode}];
  });
  await test('cos_submission_forwards_template_output_mode',async()=>{
    let body;
    const wx={getStorageSync:()=> 'fixture-token',
      getFileSystemManager:()=>({statSync:()=>({size:4}),readFile:o=>o.success({data:new ArrayBuffer(4)})}),
      request:o=>{
        let data={};
        if(o.url.endsWith('/api/config'))data={cos_ready:true};
        else if(o.url.endsWith('/api/uploads'))data={url:'https://cos.invalid/upload.jpg',upload_id:'fixture'};
        else if(o.url.endsWith('/api/rescue/by-upload')){body=o.data;data={code:0,job_id:'fixture_job'};}
        o.success({statusCode:200,data});
      }};
    await apiModule(wx,appFixture()).submitJob('/local.jpg',{quality:'fine',template_id:'poster',template_output_mode:'single'});
    return [body?.template_output_mode==='single',{mode:body?.template_output_mode,uploadId:body?.upload_id}];
  });
  await test('community_empty_category_keeps_filter_navigation',()=>{
    const xml=fs.readFileSync(path.join(ROOT,'miniprogram/pages/community/community.wxml'),'utf8');
    const empty=xml.match(/<scroll-view\s+wx:if="([^"]+)"\s+class="salon-filter-bar"/);
    const {page}=loadPage('community');
    page.setData({items:[{id:'p1',category:'portrait'}],activeFilter:'film'});
    page.filterItems('film');
    return [empty?.[1].includes('items.length') && !empty[1].includes('filteredItems') && page.data.filteredItems.length===0,
      {guard:empty?.[1],items:page.data.items.length,filtered:page.data.filteredItems.length}];
  });
  await test('javascript_syntax_and_wxml_handlers',()=>{
    const errors=[];let count=0;let bindings=0;
    const walk=dir=>fs.readdirSync(dir,{withFileTypes:true}).flatMap(e=>e.isDirectory()?walk(path.join(dir,e.name)):[path.join(dir,e.name)]);
    for(const file of walk(path.join(ROOT,'miniprogram')).filter(f=>f.endsWith('.js'))) {
      new vm.Script(fs.readFileSync(file,'utf8'),{filename:file});count++;
    }
    for(const name of fs.readdirSync(path.join(ROOT,'miniprogram/pages'))) {
      const {page}=loadPage(name);
      const xml=fs.readFileSync(path.join(ROOT,`miniprogram/pages/${name}/${name}.wxml`),'utf8');
      for(const match of xml.matchAll(/\b(?:bind(?::)?\w+|catch(?::)?\w+)=["']([a-zA-Z_$][\w$]*)["']/g)) {
        if (match[1] === 'true' || match[1] === 'false') continue;
        bindings++;
        if(typeof page[match[1]]!=='function')errors.push({page:name,handler:match[1]});
      }
    }
    for(const name of ['admin.html','index.html']) {
      const html=fs.readFileSync(path.join(ROOT,'backend',name),'utf8');
      for(const script of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)){new vm.Script(script[1]);count++;}
    }
    return [errors.length===0,{javascript_blocks:count,wxml_bindings:bindings,missing_handlers:errors}];
  });

  await test('multipart_expired_token_relogin',async()=>{
    let loginCalls=0;let uploadCalls=0;
    const app=appFixture();
    const wx={getStorageSync:()=> 'expired',setStorageSync(){},login(o){loginCalls++;o.success({code:'fixture'});},
      request(o){if(o.url.endsWith('/api/config'))o.success({statusCode:200,data:{cos_ready:false}});
        else o.success({statusCode:200,data:{token:'new-token',balance:90}});},
      uploadFile(o){uploadCalls++;o.success({statusCode:401,data:JSON.stringify({detail:'expired'})});}};
    const api=apiModule(wx,app);
    let error;
    try{await api.submitJob('/tmp/photo.jpg',{});}catch(e){error=e;}
    return [loginCalls>0,{loginCalls,uploadCalls,errorCode:error?.code}];
  });

  await test('my_jobs_ensures_login',async()=>{
    let loginCalls=0;
    const wx={getStorageSync:()=>'',setStorageSync(){},login(o){loginCalls++;o.success({code:'fixture'});},request(o){o.success(o.url.endsWith('/api/auth/login')?{statusCode:200,data:{token:'valid'}}:{statusCode:401,data:{detail:'login required'}});}};
    const api=apiModule(wx,appFixture());
    let code;
    try{await api.myJobs(20);}catch(e){code=e.code;}
    return [loginCalls>0,{loginCalls,errorCode:code}];
  });

  await test('polling_handles_unauthorized',async()=>{
    let now=0;let requestCalls=0;let loginCalls=0;
    const wx={getStorageSync:()=> 'expired',setStorageSync(){},login(o){loginCalls++;o.success({code:'fixture'});},request(o){requestCalls++;o.success(o.url.endsWith('/api/auth/login')?{statusCode:200,data:{token:'valid'}}:{statusCode:401,data:{detail:'expired'}});}};
    const api=apiModule(wx,appFixture(),{Date:{now:()=>now},setTimeout(fn,ms){now+=ms;queueMicrotask(fn);}});
    let code;
    try{await api.waitForJob('abcdef123456',{timeout:10,interval:1});}catch(e){code=e.code;}
    return [code==='UNAUTHORIZED'||loginCalls>0,{requestCalls,loginCalls,errorCode:code}];
  });

  await test('delete_work_stays_deleted',async()=>{
    const job={id:'abcdef123456',status:'succeeded',orig_url:'/orig.jpg',result_url:'/result.jpg'};
    const app=appFixture([{jobId:job.id,status:'succeeded',result:job.result_url,original:job.orig_url}]);
    let deleted=false;
    const api={deleteJob:async()=>{deleted=true;return {ok:true};},myJobs:async()=>({jobs:deleted?[]:[job]}),absolute:x=>x,request:async()=>job};
    const {page}=loadPage('works',api,app);
    page.deleteSingleWork(0);
    await tick();
    return [app.globalData.historyList.length===0,{history_after_delete_and_sync:app.globalData.historyList.length}];
  });

  await test('clear_storage_clears_memory_history',()=>{
    let app;
    vm.runInNewContext(fs.readFileSync(path.join(ROOT,'miniprogram/app.js'),'utf8'),
      {App:a=>app=a,wx:{getStorageSync:()=>'',setStorageSync(){}},console:silent});
    app.globalData.historyList=[{jobId:'old-local-job'}];
    app.onLaunch();
    return [app.globalData.historyList.length===0,{history_after_empty_storage_onLaunch:app.globalData.historyList.length}];
  });

  await test('works_refreshes_succeeded_urls',async()=>{
    const job={id:'abcdef123456',status:'succeeded',orig_url:'new-orig',result_url:'new-signed-result'};
    const app=appFixture([{jobId:job.id,status:'succeeded',original:'expired-orig',result:'expired-signed-result'}]);
    const api={myJobs:async()=>({jobs:[job]}),absolute:x=>x,request:async()=>job};
    const {page}=loadPage('works',api,app);
    await page.loadWorks();
    const current=app.globalData.historyList[0];
    return [current.result===job.result_url&&current.original===job.orig_url,{original:current.original,result:current.result}];
  });

  await test('empty_template_response_clears_cached_items',async()=>{
    const cached={id:'deleted-template',name:'old',engine:'light',price:1};
    const api={templates:async()=>({groups:[],items:[]}),config:async()=>({free_mode:false,prices:{light:1,fine:3}}),absolute:x=>x};
    const {page}=loadPage('templates',api,appFixture(),{getStorageSync:()=>({groups:[],items:[cached]})});
    page.fetchTemplates();await tick();
    return [page.data.allTemplates.length===0,{templates_after_empty_response:page.data.allTemplates.map(t=>t.id)}];
  });

  await test('adjust_stops_polling_on_hide',async()=>{
    let resolveWait;let redirected=0;
    const app=appFixture();
    const api={submitJob:async()=>({code:0,job_id:'abcdef123456',balance:87,orig_url:'orig'}),absolute:x=>x,
      waitForJob:async(_,options)=>new Promise(r=>{resolveWait=()=>r({status:'succeeded',orig_url:'orig',result_url:'result'});})};
    const {page}=loadPage('adjust',api,app,{redirectTo(){redirected++;}});
    page.data.imagePath='fixture.jpg';
    const generating=page.executeUpload('fixture.jpg');await tick();
    if(page.onHide)page.onHide();
    resolveWait();await generating;
    return [redirected===0,{has_onHide:typeof page.onHide==='function',has_onUnload:typeof page.onUnload==='function',redirects_after_hidden_completion:redirected}];
  });

  await test('web_timeout_resets_processing_ui',async()=>{
    const html=fs.readFileSync(path.join(ROOT,'backend/index.html'),'utf8');
    const script=[...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)][0][1];
    const elements={};
    const el=id=>elements[id]||= {style:{},classList:{add(){},remove(){}},files:[],click(){}};
    let nowCall=0;let alerts=[];
    const sandbox={document:{getElementById:el},window:{},console:silent,
      localStorage:{getItem:()=> 'valid-token'},URL:{createObjectURL:()=> 'blob:fixture'},
      FormData:class{append(){}},Date:{now:()=>nowCall++===0?0:180001},
      setTimeout:fn=>queueMicrotask(fn),alert:message=>alerts.push(message),
      fetch:async url=>({ok:true,status:200,json:async()=>url==='/api/auth/web'?{token:'valid-token'}:
        url==='/api/rescue'?{code:0,job_id:'abcdef123456'}:{status:'processing'}})};
    vm.runInNewContext(script,sandbox);
    el('fileInput').files=[{name:'fixture.jpg',size:700}];
    el('fileInput').onchange();
    await el('btnRescue').onclick();
    return [el('processingMask').style.display==='none',{mask:el('processingMask').style.display,controls:el('readyControls').style.display,alerts}];
  });

  await test('expired_token_upload_recovers_successfully',async()=>{
    let token='expired',logins=0,uploads=0;
    const app=appFixture();
    const wx={getStorageSync:()=>token,setStorageSync(_,value){token=value;},
      login(o){logins++;o.success({code:'fixture'});},
      request(o){o.success(o.url.endsWith('/api/auth/login')?{statusCode:200,data:{token:'renewed',balance:90}}:{statusCode:200,data:{cos_ready:false}});},
      uploadFile(o){uploads++;o.success(o.header.Authorization==='Bearer renewed'?{statusCode:200,data:JSON.stringify({code:0,job_id:'abcdef123456',balance:87})}:{statusCode:401,data:JSON.stringify({detail:'expired'})});}};
    const data=await apiModule(wx,app).submitJob('fixture.jpg',{});
    return [data.code===0&&logins===1&&uploads===2&&app.globalData.lightPoints===87,{logins,uploads,balance:app.globalData.lightPoints}];
  });

  await test('admin_dialog_backdrop_and_layout_and_bulk_delete_controls',()=>{
    const html=fs.readFileSync(path.join(ROOT,'backend/admin.html'),'utf8');
    const wxss=fs.readFileSync(path.join(ROOT,'miniprogram/pages/compare/compare.wxss'),'utf8');
    const ok=!html.includes('onclick="maskClose(event)"') &&
      !html.includes('<button class="btn-ghost" onclick="closeModal()">取消</button>') &&
      html.includes('onclick="openPurgeUsersModal()"') &&
      html.includes("confirmation:'移出全部用户'") && !html.includes('永久删除全部账号') &&
      html.includes('input[type=url]') &&
      html.includes('.form-grid > .field > label') &&
      html.includes('loadViolationFeedback()') &&
      /\.dock-btn\s*\{[^}]*flex:\s*1/.test(wxss) &&
      /\.dock-btn-save\s*\{[^}]*flex:\s*1/.test(wxss);
    return [ok,{modal_backdrop_closes:false,bulk_delete_control:html.includes('openPurgeUsersModal()'),equal_compare_buttons:ok}];
  });

  await test('credit_packages_and_custom_violation_dialog_are_rendered',async()=>{
    const xml=fs.readFileSync(path.join(ROOT,'miniprogram/pages/adjust/adjust.wxml'),'utf8');
    const css=fs.readFileSync(path.join(ROOT,'miniprogram/pages/adjust/adjust.wxss'),'utf8');
    const {page}=loadPage('credits',{config:async()=>({free_mode:false}),me:async()=>({balance:100,earn:{}}),
      request:async()=>({generation_cost:40,payment_ready:false,packages:[{id:'points_600',yuan:6,points:600,generations:15}]})});
    page.onLoad();page.onShow();await tick();await tick();
    return [page.data.costPerGeneration===40&&page.data.estimatedGenerations===2&&
      page.data.packages[0].points===600&&xml.includes('showViolationNotice')&&
      css.includes('.violation-mask')&&!xml.includes('class="feedback-sheet"'),
      {cost:page.data.costPerGeneration,images:page.data.estimatedGenerations,custom_dialog:true}];
  });

  await test('checkin_calendar_and_customer_service_are_real_actions',async()=>{
    let claims=0;
    const {page}=loadPage('credits',{request:async()=>({packages:[],generation_cost:40}),
      config:async()=>({free_mode:false,rewards:{checkin:10,checkin_seventh_bonus:30,invite:40}}),
      me:async()=>({balance:100,earn:{checkin_done:false,checkin_streak:6,checkin_progress:6,checkin_day:7}}),
      earn:async()=>{claims++;return {balance:140,reward:40,checkin_streak:7,checkin_progress:7};}});
    page.onShow();await tick();await tick();page.onOpenCheckin();page.onCheckIn();await tick();await tick();
    const creditsXml=fs.readFileSync(path.join(ROOT,'miniprogram/pages/credits/credits.wxml'),'utf8');
    const myXml=fs.readFileSync(path.join(ROOT,'miniprogram/pages/my/my.wxml'),'utf8');
    const adjustXml=fs.readFileSync(path.join(ROOT,'miniprogram/pages/adjust/adjust.wxml'),'utf8');
    return [claims===1&&page.data.checkinDays.length===7&&page.data.checkedIn&&
      creditsXml.includes('onOpenCheckin')&&myXml.includes('open-type="contact"')&&
      adjustXml.includes('open-type="contact"'),{claims,days:page.data.checkinDays.length,contact:true}];
  });

  await test('profile_contact_button_aligns_with_other_menu_rows',()=>{
    const xml=fs.readFileSync(path.join(ROOT,'miniprogram/pages/my/my.wxml'),'utf8');
    const css=fs.readFileSync(path.join(ROOT,'miniprogram/pages/my/my.wxss'),'utf8');
    const wrapped=/<view class="menu-item">\s*<button class="menu-contact-action" open-type="contact"[^>]*>/.test(xml);
    const rule=/\.menu-contact-action\s*\{([^}]*)\}/.exec(css)?.[1]||'';
    const aligned=wrapped&&/flex:\s*1\s*;/.test(rule)&&/margin:\s*0\s*;/.test(rule)&&
      /padding:\s*0\s*;/.test(rule)&&/text-align:\s*left\s*;/.test(rule)&&
      !/\.menu-contact\s*\{[^}]*width:\s*100%/.test(css);
    return [aligned,{wrapped,button_style:rule.trim().slice(0,120)}];
  });

  await test('downloaded_result_is_reused_in_session_media_cache',async()=>{
    const app=appFixture();
    const api=apiModule({downloadFile:o=>o.success({statusCode:200,tempFilePath:'wxfile://cached-result.jpg'}),
      getImageInfo:o=>o.success({width:90,height:160})},app);
    const path=await api.downloadJobMedia('abcdef123456','result','https://cos.invalid/result.jpg');
    return [path==='wxfile://cached-result.jpg'&&
      app.globalData.mediaCache['abcdef123456'].result===path,{cached:path}];
  });

  await test('works_reentry_reuses_thumbnail_and_skips_duplicate_fetch',async()=>{
    let calls=0;
    const app=appFixture([{jobId:'id1',result:'https://cos.invalid/old',preview:'https://cos.invalid/old',status:'succeeded'}]);
    const {page}=loadPage('works',{myJobs:async()=>{calls++;return {jobs:[{id:'id1',status:'succeeded',result_url:'https://cos.invalid/new'}]};},
      absolute:x=>x,isJobCosUrl:x=>x.startsWith('https://cos.invalid/')},app);
    page.onShow();await tick();await tick();const first=page.data.works[0].preview;
    page.onShow();await tick();
    return [calls===1&&first==='https://cos.invalid/old'&&page.data.works[0].preview===first,
      {calls,preview:first}];
  });

  await test('media_preview_cache_rejects_expired_cos_signatures',()=>{
    const api=apiModule({},appFixture(),{Date:{now:()=>200000}});
    if(typeof api.isReusableMediaUrl!=='function')return [false,{helper:'missing'}];
    const expired=api.isReusableMediaUrl('https://cos.invalid/a.jpg?q-sign-time=100;190');
    const valid=api.isReusableMediaUrl('https://cos.invalid/a.jpg?q-sign-time=100%3B600');
    const local=api.isReusableMediaUrl('http://tmp/cached.jpg');
    return [!expired&&valid&&local,{expired,valid,local}];
  });

  await test('works_replaces_expired_pinned_thumbnail_with_fresh_url',async()=>{
    let calls=0;
    const old='https://cos.invalid/expired.jpg',fresh='https://cos.invalid/fresh.jpg';
    const app=appFixture([{jobId:'id1',status:'succeeded',result:old,preview:old}]);
    const {page}=loadPage('works',{myJobs:async()=>{calls++;return {jobs:[{id:'id1',status:'succeeded',result_url:fresh}]};},
      absolute:x=>x,isJobCosUrl:x=>x.startsWith('https://cos.invalid/'),isReusableMediaUrl:x=>!!x&&!x.includes('expired')},app);
    page.data.works=app.globalData.historyList;page._lastLoadedAt=Date.now();await page.loadWorks();
    return [calls===1&&page.data.works[0].preview===fresh,{calls,preview:page.data.works[0].preview}];
  });

  await test('profile_replaces_expired_pinned_thumbnail_with_fresh_url',async()=>{
    let calls=0;
    const old='https://cos.invalid/expired.jpg',fresh='https://cos.invalid/fresh.jpg';
    const app=appFixture([{jobId:'id1',status:'succeeded',result:old,preview:old}]);
    const {page}=loadPage('my',{config:async()=>({free_mode:true,ads:{}}),me:async()=>({balance:90}),
      myJobs:async()=>{calls++;return {jobs:[{id:'id1',status:'succeeded',result_url:fresh}]};},
      absolute:x=>x,isJobCosUrl:()=>true,isReusableMediaUrl:x=>!!x&&!x.includes('expired')},app);
    page.data.historyList=app.globalData.historyList;page.data.previewWorks=app.globalData.historyList;
    page._lastProfileSync=Date.now();page._lastHistoryKeys='id1';page.refreshUserData();await tick();await tick();
    return [calls===1&&page.data.previewWorks[0].preview===fresh,{calls,preview:page.data.previewWorks[0].preview}];
  });

  await test('visible_pending_work_auto_syncs_and_poll_stops_on_hide',async()=>{
    let done=false,nextId=1;const timers=new Map();
    const clock={setTimeout(fn){const id=nextId++;timers.set(id,fn);return id;},clearTimeout(id){timers.delete(id);}};
    const job={id:'id1',status:'processing',quality:'light'};
    const api={myJobs:async()=>({jobs:[job]}),absolute:x=>x,
      request:async()=>({...job,status:done?'succeeded':'processing',result_url:done?'cos-result':''})};
    const {page}=loadPage('works',api,appFixture(),{},clock);
    page.onShow();await tick();await tick();
    if(timers.size===0)return [false,{scheduled:false}];
    const callback=[...timers.values()][0];timers.clear();done=true;await callback();await tick();
    const synced=page.data.works[0]?.status==='succeeded';
    done=false;page.data.works=[{jobId:'id1',status:'processing'}];
    if(typeof page.schedulePendingRefresh==='function')page.schedulePendingRefresh();
    const scheduledBeforeHide=timers.size;
    if(typeof page.onHide==='function')page.onHide();
    return [synced&&scheduledBeforeHide===1&&timers.size===0,
      {synced,scheduledBeforeHide,scheduledAfterHide:timers.size}];
  });

  await test('nickname_requires_user_input_and_persists_server_reply',async()=>{
    const {page}=loadPage('my',{config:async()=>({free_mode:false,ads:{},prices:{light:40,fine:40}}),
      myJobs:async()=>({jobs:[]}),me:async()=>({user_id:'WX-0123456789ABCDEF',balance:100,nickname:'小山',earn:{}}),
      updateProfile:async(name)=>({nickname:name})});
    page.onShow();await tick();await tick();page.onEditProfile();
    page.onProfileInput({detail:{value:'阿星'}});await page.onSaveProfile();
    const xml=fs.readFileSync(path.join(ROOT,'miniprogram/pages/my/my.wxml'),'utf8');
    return [page.data.nickName==='阿星'&&!page.data.showProfileEditor&&xml.includes('type="nickname"'),
      {nickname:page.data.nickName,user_action:true}];
  });

  await test('works_repair_old_server_media_without_loading_photo_bytes_from_api',async()=>{
    const app=appFixture([]);let repaired=0;
    const api={myJobs:async()=>({jobs:[{id:'abcdef123456',status:'succeeded',result_url:'/api/images/result.jpg',orig_url:'/api/images/orig.jpg'}]}),
      request:async()=>({status:'succeeded'}),absolute:x=>x,
      isJobCosUrl:x=>typeof x==='string'&&x.startsWith('https://cos.invalid/'),
      repairJobMedia:async()=>{repaired++;return 'https://cos.invalid/recovered.jpg';}};
    const {page}=loadPage('works',api,app);await page.loadWorks();
    const work=page.data.works[0];
    return [repaired===1&&work.result==='https://cos.invalid/recovered.jpg'&&work.preview===work.result,
      {repaired,result:work.result,preview:work.preview}];
  });

  await test('works_fullscreen_preview_uses_local_file_from_cos',async()=>{
    let opened,downloads=0;
    const {page}=loadPage('works',{downloadJobMedia:async()=>{downloads++;return 'wxfile://cos-result.jpg';}},appFixture(),{
      showActionSheet:o=>o.success({tapIndex:0}),previewImage:o=>{opened=o;}
    });
    page.showWorkActions({jobId:'abcdef123456',result:'https://cos.invalid/signed.jpg',original:'/api/images/orig.jpg'},0);
    await tick();
    return [downloads===1&&opened?.current==='wxfile://cos-result.jpg'&&opened?.urls.length===1,
      {downloads,opened}];
  });

  await test('works_thumbnail_error_has_bounded_cos_repair',async()=>{
    let repaired=0;
    const api={repairJobMedia:async()=>{repaired++;return 'https://cos.invalid/fresh.jpg';},
      isJobCosUrl:x=>String(x).startsWith('https://cos.invalid/')};
    const {page}=loadPage('works',api);page.data.works=[{jobId:'abcdef123456',status:'succeeded',result:'https://cos.invalid/old.jpg',preview:'https://cos.invalid/old.jpg'}];
    if(typeof page.onWorkImageError!=='function')return [false,{handler:'missing'}];
    page.onWorkImageError({currentTarget:{dataset:{index:0}}});await tick();
    page.onWorkImageError({currentTarget:{dataset:{index:0}}});await tick();
    const xml=fs.readFileSync(path.join(ROOT,'miniprogram/pages/works/works.wxml'),'utf8');
    return [repaired===1&&page.data.works[0].preview==='https://cos.invalid/fresh.jpg'&&xml.includes('binderror="onWorkImageError"'),
      {repaired,preview:page.data.works[0].preview}];
  });

  await test('profile_thumbnail_never_uses_old_backend_image_route',async()=>{
    const app=appFixture([]);
    const {page}=loadPage('my',{config:async()=>({free_mode:true,ads:{}}),me:async()=>({balance:90}),
      myJobs:async()=>({jobs:[{id:'abcdef123456',status:'succeeded',result_url:'/api/images/result.jpg'}]}),
      absolute:x=>x,isJobCosUrl:()=>false},app);
    page.onShow();await tick();await tick();
    const xml=fs.readFileSync(path.join(ROOT,'miniprogram/pages/my/my.wxml'),'utf8');
    return [page.data.previewWorks[0]?.preview===''&&xml.includes('src="{{ item.preview }}"'),
      {preview:page.data.previewWorks[0]?.preview}];
  });

  await test('legacy_work_without_job_id_never_previews_backend_image',async()=>{
    let previewCalls=0,modal;
    const app=appFixture([{status:'succeeded',result:'/api/images/legacy.jpg',original:'/api/images/orig.jpg'}]);
    const {page}=loadPage('works',{myJobs:async()=>({jobs:[]}),absolute:x=>x,isJobCosUrl:()=>false},app,{
      showActionSheet:o=>o.success({tapIndex:0}),previewImage:()=>previewCalls++,showModal:o=>{modal=o;}
    });
    await page.loadWorks();page.showWorkActions(page.data.works[0],0);
    return [page.data.works[0].preview===''&&previewCalls===0&&modal?.title==='图片暂不可用',
      {thumbnail:page.data.works[0].preview,previewCalls,modal:modal?.title}];
  });

  await test('catalog_respects_free_mode_and_current_server_prices',async()=>{
    const data={groups:[],items:[{id:'light',engine:'light',price:0},{id:'fine',engine:'fine',price:0},{id:'custom',engine:'fine',price:7}]};
    const app=appFixture();
    const {page}=loadPage('templates',{templates:async()=>data,config:async()=>({free_mode:true,prices:{light:2,fine:5}})},app);
    page.onLoad();page.onShow();await tick();await tick();
    const free=page.data.filteredTemplates.map(t=>t.costText);
    return [free.every(c=>c==='免扣费'),{free_labels:free}];
  });

  await test('home_reprices_cached_templates_after_late_config',async()=>{
    let resolveConfig;const pending=new Promise(r=>resolveConfig=r);
    const {page}=loadPage('index',{templates:async()=>({items:[{id:'fine',engine:'fine',price:0}]}),
      config:()=>pending,me:async()=>({balance:90}),announcements:async()=>({items:[]})});
    page.onLoad({});page.onShow();await tick();
    resolveConfig({free_mode:true,prices:{light:2,fine:5}});await tick();await tick();
    return [page.data.featuredTemplates[0]?.costText==='免扣费',
      {label:page.data.featuredTemplates[0]?.costText,free_mode:page.data.freeMode}];
  });

  await test('removed_catalog_group_falls_back_to_all',async()=>{
    const {page}=loadPage('templates');page.data.activeCategory='removed';
    page._renderData([],[{id:'kept',engine:'light',group_id:'new'}]);
    return [page.data.activeCategory==='all'&&page.data.filteredTemplates.length===1,
      {category:page.data.activeCategory,shown:page.data.filteredTemplates.length}];
  });

  await test('superseded_catalog_request_ends_pull_to_refresh',async()=>{
    const pending=[];let stopped=0;
    const {page}=loadPage('templates',{templates:()=>new Promise(resolve=>pending.push(resolve)),
      config:async()=>({free_mode:false,prices:{light:1,fine:3}}),absolute:x=>x},appFixture(),
      {stopPullDownRefresh:()=>stopped++});
    page.onPullDownRefresh();page.onShow();
    pending[0]({groups:[],items:[{id:'stale'}]});await tick();
    pending[1]({groups:[],items:[{id:'current',engine:'light'}]});await tick();
    return [stopped===1&&page.data.filteredTemplates.map(t=>t.id).join()==='current',
      {stopped,shown:page.data.filteredTemplates.map(t=>t.id)}];
  });

  await test('community_share_uses_post_image_not_project_logo',async()=>{
    const result='https://photos.invalid/post.jpg';let requested,shared;
    const {page}=loadPage('community',{},appFixture(),{
      getImageInfo:o=>{requested=o.src;o.success({path:'wxfile://post.jpg'});},
      showShareImageMenu:o=>{shared=o.path;}
    });
    page.onShareExhibit({currentTarget:{dataset:{resultUrl:result,title:'展品'}}});
    await tick();
    const wxml=fs.readFileSync(path.join(ROOT,'miniprogram/pages/community/community.wxml'),'utf8');
    return [requested===result&&shared==='wxfile://post.jpg'&&wxml.includes('data-result-url="{{ item.resultUrl }}"'),
      {requested,shared}];
  });

  await test('invite_failure_never_exposes_fake_redeemable_code',async()=>{
    let copied,toast;
    const {page}=loadPage('invite',{me:async()=>{throw new Error('offline');}},appFixture(),{
      setClipboardData:o=>{copied=o.data;},showToast:o=>{toast=o.title;}
    });
    page.onLoad();await tick();page.onCopyCode();
    const share=page.onShareAppMessage();
    return [!copied&&!share.path.includes('invite=')&&page.data.inviteCode==='',
      {code:page.data.inviteCode,share_path:share.path,copied,toast}];
  });

  await test('credits_and_invite_pages_do_not_fabricate_account_history',()=>{
    const credits=loadPage('credits').page;
    const inviteXml=fs.readFileSync(path.join(ROOT,'miniprogram/pages/invite/invite.wxml'),'utf8');
    const creditsXml=fs.readFileSync(path.join(ROOT,'miniprogram/pages/credits/credits.wxml'),'utf8');
    const passed=credits.data.records.length===0&&!inviteXml.includes('2026 年 9 月 28 日')&&
      !inviteXml.includes('待首次成功生成')&&creditsXml.includes('暂无光子明细');
    return [passed,{default_records:credits.data.records.length,has_fake_invite:inviteXml.includes('2026 年 9 月 28 日')}];
  });

  await test('profile_syncs_cloud_works_and_current_prices',async()=>{
    const app=appFixture([]);
    const {page}=loadPage('my',{config:async()=>({free_mode:false,prices:{light:2,fine:5},ads:{}}),
      me:async()=>({user_id:'WX-0123456789ABCDEF',balance:90,earn:{}}),absolute:x=>x,
      myJobs:async()=>({jobs:[{id:'first',status:'succeeded',result_url:'cos-a',quality:'light'},
        {id:'second',status:'processing',quality:'fine'}]})},app);
    page.onLoad();page.onShow();await tick();await tick();
    const xml=fs.readFileSync(path.join(ROOT,'miniprogram/pages/my/my.wxml'),'utf8');
    return [page.data.historyList.length===2&&page.data.processingCount===1&&
      page.data.priceLight===2&&page.data.priceFine===5&&xml.includes('priceLight'),
      {cloud_works:page.data.historyList.length,processing:page.data.processingCount,
        light_price:page.data.priceLight,fine_price:page.data.priceFine}];
  });

  await test('concurrent_login_single_flight',async()=>{
    let token='',logins=0,finish;
    const wx={getStorageSync:()=>token,setStorageSync(_,value){token=value;},
      login(o){logins++;finish=()=>o.success({code:'fixture'});},
      request(o){o.success({statusCode:200,data:{token:'new'}});}};
    const api=apiModule(wx,appFixture());
    const waits=[api.ensureLogin(),api.ensureLogin(),api.ensureLogin(true)];
    finish();const tokens=await Promise.all(waits);
    return [logins===1&&tokens.every(x=>x==='new'),{logins,tokens}];
  });

  await test('polling_404_is_terminal',async()=>{
    let calls=0;
    const api=apiModule({getStorageSync:()=> 'valid',request(o){calls++;o.success({statusCode:404,data:{detail:'deleted'}});}},appFixture());
    let status;try{await api.waitForJob('abcdef123456',{interval:1,timeout:50});}catch(e){status=e.status;}
    return [calls===1&&status===404,{calls,status}];
  });

  await test('uncertain_cos_submission_never_double_submits',async()=>{
    let uploads=0;
    const wx={getStorageSync:()=> 'valid',getFileSystemManager:()=>({statSync:()=>({size:700}),readFile:o=>o.success({data:new ArrayBuffer(700)})}),
      request(o){
        if(o.url.endsWith('/api/rescue/by-upload'))return o.fail({errMsg:'request:fail timeout'});
        if(o.url.endsWith('/api/config'))return o.success({statusCode:200,data:{cos_ready:true}});
        if(o.url.endsWith('/api/uploads'))return o.success({statusCode:200,data:{upload_id:'fixture',url:'https://fixture.invalid/cos'}});
        o.success({statusCode:200,data:{ok:true}});
      },uploadFile(){uploads++;}};
    let code;try{await apiModule(wx,appFixture()).submitJob('fixture.png',{});}catch(e){code=e.code;}
    return [uploads===0&&code==='NETWORK',{multipart_calls:uploads,errorCode:code}];
  });

  await test('delete_error_keeps_work_and_reports_failure',async()=>{
    const app=appFixture([{jobId:'abcdef123456',result:'result',status:'succeeded'}]);
    let toast='';
    const {page}=loadPage('works',{deleteJob:async()=>{throw new Error('offline');}},app,{showToast:o=>toast=o.title});
    page.deleteSingleWork(0);await tick();
    return [app.globalData.historyList.length===1&&toast==='offline',{history:app.globalData.historyList.length,toast}];
  });

  await test('clear_all_deletes_cloud_before_sync',async()=>{
    let cloud=[{id:'abcdef123456',result_url:'result',status:'succeeded'}];
    const app=appFixture([{jobId:cloud[0].id,status:'succeeded',result:'result'}]);
    const api={deleteAllJobs:async()=>{cloud=[];return {ok:true};},myJobs:async()=>({jobs:cloud}),absolute:x=>x};
    const {page}=loadPage('works',api,app);
    page.onClearAllWorks();await tick();
    return [!cloud.length&&!app.globalData.historyList.length,{cloud:cloud.length,local:app.globalData.historyList.length}];
  });

  await test('empty_home_templates_clear_featured',async()=>{
    const {page}=loadPage('index',{templates:async()=>({items:[]}),absolute:x=>x});
    page.loadTemplates();await tick();
    return [page.data.featuredTemplates.length===0,{featured:page.data.featuredTemplates.length}];
  });

  await test('compare_uses_real_pixels_and_expired_original_state',async()=>{
    const {page}=loadPage('compare',{request:async()=>({status:'succeeded',orig_url:null,result_url:'result',provider:'local',width:1536,height:1024}),absolute:x=>x,downloadJobMedia:async(id,kind,url)=>url});
    page._jobId='abcdef123456';await page.refreshUrls();
    return [page.data.originalUnavailable&&page.data.originalUrl==='result'&&page.data.label.includes('1536'),{label:page.data.label,originalUnavailable:page.data.originalUnavailable}];
  });

  await test('account_display_id_survives_relogin_after_cache_clear',async()=>{
    let app;
    const storage={'studioUserId':'PX-OLD-RANDOM'};
    const uid='WX-0123456789ABCDEF';
    const wx={getStorageSync:k=>storage[k]||'',setStorageSync:(k,v)=>storage[k]=v,
      getAccountInfoSync:()=>({miniProgram:{appId:'wx_test_a'}}),
      login:o=>o.success({code:'same-user-code'}),
      request:o=>o.success({statusCode:200,data:o.url.endsWith('/api/auth/login')?{token:'session',user_id:uid,balance:90}:{user_id:uid,balance:90}})};
    vm.runInNewContext(fs.readFileSync(path.join(ROOT,'miniprogram/app.js'),'utf8'),{App:a=>app=a,wx,console:silent});
    app.onLaunch();const before=app.globalData.userId;
    await apiModule(wx,app).me();const first=app.globalData.userId;
    Object.keys(storage).forEach(k=>delete storage[k]);app.onLaunch();
    await apiModule(wx,app).me();const second=app.globalData.userId;
    return [before===''&&first===uid&&second===uid,{legacy_random_id_discarded:before==='',first,second}];
  });

  await test('wechat_login_sends_real_appid_and_syncs_server_id',async()=>{
    let payload;const app=appFixture();
    app.setUserIdentity=id=>app.globalData.userId=id;
    const wx={getStorageSync:()=>'',setStorageSync(){},getAccountInfoSync:()=>({miniProgram:{appId:'wx_real_app'}}),
      login:o=>o.success({code:'code'}),request:o=>{payload=o.data;o.success({statusCode:200,data:{token:'t',user_id:'WX-0123456789ABCDEF',balance:90}});}};
    await apiModule(wx,app).ensureLogin();
    return [payload.app_id==='wx_real_app'&&app.globalData.userId==='WX-0123456789ABCDEF',{app_id:payload.app_id,user_id:app.globalData.userId}];
  });

  await test('web_bootstrap_preserves_legacy_auth_token_for_cookie',async()=>{
    const html=fs.readFileSync(path.join(ROOT,'backend/index.html'),'utf8');
    const script=[...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)][0][1];
    const elements={};const el=id=>elements[id]||= {style:{},classList:{add(){},remove(){}},files:[],click(){}};
    let options;
    const sandbox={document:{getElementById:el},window:{},console:silent,
      localStorage:{getItem:()=> 'old-valid-token',setItem(){}},URL:{createObjectURL:()=> 'blob:fixture'},
      FormData:class{append(){}},Date,setTimeout,
      fetch:async(url,opts)=>{options=opts;return {ok:true,status:200,json:async()=>({token:'renewed',balance:245})};}};
    vm.runInNewContext(script,sandbox);await sandbox.ensureWebToken();
    return [options.headers.Authorization==='Bearer old-valid-token'&&options.credentials==='same-origin',
      {legacy_authorization:options.headers.Authorization,credentials:options.credentials}];
  });

  await test('admin_users_default_wechat_filter_shows_fixed_id_and_counts',async()=>{
    const html=fs.readFileSync(path.join(ROOT,'backend/admin.html'),'utf8');
    const start=html.indexOf('async function loadUsers() {');
    const end=html.indexOf('/* ---------- 用户操作',start);
    const script=html.slice(start,end);
    const elements={'users-source':{value:'wechat'},'users-summary':{},'users-table':{tBodies:[{rows:[],appendChild(row){this.rows.push(row);}}]}};
    let requested;
    const sandbox={$:id=>elements[id],console:silent,esc:v=>String(v),fmtTime:()=> 'time',document:{createElement:()=>({})},
      api:async url=>{requested=url;return {stats:{wechat_users_total:1,web_users_total:8},items:[{openid:'o_real_wechat_user',user_id:'WX-0123456789ABCDEF',account_type:'wechat',app_id:'wx_test',total_jobs:2,blocked:0,balance:110}]};}};
    vm.runInNewContext(script,sandbox);await sandbox.loadUsers();
    const row=elements['users-table'].tBodies[0].rows[0].innerHTML;
    return [requested.includes('account_type=wechat')&&row.includes('WX-0123456789ABCDEF')&&elements['users-summary'].textContent.includes('网页访客 8'),
      {request:requested,summary:elements['users-summary'].textContent,fixed_id_displayed:row.includes('WX-0123456789ABCDEF')}];
  });

  const failed=results.filter(x=>!x.passed).length;
  console.log(`FRONTEND_SUMMARY total=${results.length} passed=${results.length-failed} failed=${failed}`);
  fs.writeFileSync(path.join(HERE,'frontend_results.json'),JSON.stringify(results,null,2),'utf8');
  process.exitCode=failed?1:0;
})();
