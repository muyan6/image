/* Targeted wx/DOM execution for media caching, template framing and light defaults. */
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const root=path.resolve(process.env.REVIEW_ROOT||path.resolve(__dirname,'../..'));
const output=path.resolve(process.env.REVIEW_OUTPUT||path.join(root,'audit/workflow-tests'));
fs.mkdirSync(output,{recursive:true});
const rows=[],tick=()=>new Promise(r=>setImmediate(r));
const app=()=>({globalData:{apiBase:'https://server.invalid',historyList:[],lightPoints:90},setBalance(){},persist(){}});
function page(name,api={},wx={}) {
  let p;
  const mocks={showToast(){},showModal(){},showLoading(){},hideLoading(){},navigateTo(){},vibrateShort(){},getWindowInfo:()=>({windowWidth:375}),...wx};
  vm.runInNewContext(fs.readFileSync(path.join(root,`miniprogram/pages/${name}/${name}.js`),'utf8'),
    {getApp:app,require:()=>api,Page:x=>p=x,wx:mocks,setTimeout,console});
  p.data=JSON.parse(JSON.stringify(p.data));p.setData=d=>Object.assign(p.data,d);return p;
}
function apiModule(wx) {
  const sandbox={module:{exports:{}},getApp:app,wx,setTimeout,console};
  vm.runInNewContext(fs.readFileSync(path.join(root,'miniprogram/utils/api.js'),'utf8'),sandbox);return sandbox.module.exports;
}
async function test(name,fn) {
  try{await fn();rows.push({case:name,passed:true});console.log('PASS '+name);}
  catch(e){rows.push({case:name,passed:false,error:String(e.stack)});console.log('FAIL '+name+': '+e.stack);}
}
const job={id:'abcdef123456',status:'succeeded',orig_url:'https://cos.invalid/orig?signature=fresh',
  result_url:'https://cos.invalid/result?signature=fresh',width:90,height:160};
(async()=>{
  await test('fresh_cos_download_is_local_and_no_token_is_sent_to_cos',async()=>{
    let header,url;
    const api=apiModule({downloadFile:o=>{header=o.header;url=o.url;o.success({statusCode:200,tempFilePath:'wxfile://result.jpg'});},
      getImageInfo:o=>o.success({width:90,height:160})});
    assert.equal(await api.downloadJobMedia(job.id,'result',job.result_url),'wxfile://result.jpg');
    assert.equal(url,job.result_url);assert.equal(header.Authorization,undefined);
  });
  await test('cos_403_refreshes_signature_but_both_downloads_stay_on_cos',async()=>{
    const calls=[];
    const api=apiModule({getStorageSync:()=> 'session',request:o=>o.success({statusCode:200,data:{...job,result_url:'https://cos.invalid/result?signature=renewed'}}),downloadFile:o=>{calls.push(o);o.success(calls.length===1?
      {statusCode:403}:{statusCode:200,tempFilePath:'wxfile://fallback.jpg'});},getImageInfo:o=>o.success({})});
    assert.equal(await api.downloadJobMedia(job.id,'result',job.result_url),'wxfile://fallback.jpg');
    assert.equal(calls[0].header.Authorization,undefined);assert.equal(calls[1].header.Authorization,undefined);
    assert.equal(calls[1].url,'https://cos.invalid/result?signature=renewed');
  });
  await test('metadata_401_relogs_in_once_but_photo_never_uses_api_server',async()=>{
    let token='expired',downloads=0,logins=0;
    const api=apiModule({getStorageSync:()=>token,setStorageSync:(k,v)=>{token=v;},login:o=>{logins++;o.success({code:'fixture'});},
      request:o=>o.success(o.url.endsWith('/api/auth/login')?{statusCode:200,data:{token:'fresh'}}:
        token==='expired'?{statusCode:401,data:{detail:'expired'}}:{statusCode:200,data:job}),getImageInfo:o=>o.success({}),
      downloadFile:o=>{assert(o.url.startsWith('https://cos.invalid/'));downloads++;o.success({statusCode:200,tempFilePath:'wxfile://ok.jpg'});}});
    assert.equal(await api.downloadJobMedia(job.id,'result'),'wxfile://ok.jpg');assert.equal(logins,1);assert.equal(downloads,1);
  });
  await test('http_200_html_image_is_reported_no_server_fallback',async()=>{
    let count=0;
    const api=apiModule({getStorageSync:()=> 'token',downloadFile:o=>{count++;o.success({statusCode:200,tempFilePath:count===1?'bad.html':'good.jpg'});},
      getImageInfo:o=>o.src==='bad.html'?o.fail({}):o.success({})});
    await assert.rejects(api.downloadJobMedia(job.id,'result',job.result_url),/图片数据异常/);assert.equal(count,1);
  });
  await test('server_picture_urls_only_request_repair_json_then_download_cos',async()=>{
    let downloads=0,repairs=0;
    const api=apiModule({getStorageSync:()=> 'session',request:o=>{assert(o.url.includes('/refresh-media'));repairs++;o.success({statusCode:200,data:{url:job.result_url}});},
      getImageInfo:o=>o.success({}),downloadFile:o=>{assert(o.url.startsWith('https://cos.invalid/'));downloads++;o.success({statusCode:200,tempFilePath:'wxfile://cos.jpg'});}});
    for(const url of ['/api/images/result.jpg','https://server.invalid/api/images/result.jpg'])
      assert.equal(await api.downloadJobMedia(job.id,'result',url),'wxfile://cos.jpg');
    assert.equal(downloads,2);assert.equal(repairs,2);
  });
  await test('cos_404_recovers_missing_object_then_downloads_cos_not_server',async()=>{
    let downloads=0,repairs=0;
    const api=apiModule({getStorageSync:()=> 'session',request:o=>{assert(o.url.includes('/refresh-media'));assert.equal(o.method,'POST');repairs++;o.success({statusCode:200,data:{url:'https://cos.invalid/recovered.jpg'}});},
      getImageInfo:o=>o.success({}),downloadFile:o=>{assert(o.url.startsWith('https://cos.invalid/'));downloads++;o.success(downloads===1?{statusCode:404}:{statusCode:200,tempFilePath:'wxfile://cos.jpg'});}});
    assert.equal(await api.downloadJobMedia(job.id,'result',job.result_url),'wxfile://cos.jpg');assert.equal(downloads,2);assert.equal(repairs,1);
  });
  await test('cos_403_retry_is_bounded_and_not_a_proxy',async()=>{
    let downloads=0,requests=0;
    const api=apiModule({getStorageSync:()=> 'session',request:o=>{requests++;o.success({statusCode:200,data:job});},
      downloadFile:o=>{assert(o.url.startsWith('https://cos.invalid/'));downloads++;o.success({statusCode:403});}});
    await assert.rejects(api.downloadJobMedia(job.id,'result',job.result_url),/403/);assert.equal(downloads,2);assert.equal(requests,1);
  });
  await test('download_domain_failure_is_reported_without_retry_or_proxy',async()=>{
    let calls=0;
    const api=apiModule({downloadFile:o=>{calls++;o.fail({errMsg:'downloadFile:fail url not in domain list'});}});
    await assert.rejects(api.downloadJobMedia(job.id,'result',job.result_url),/COS 下载域名未配置：cos.invalid/);assert.equal(calls,1);
  });
  await test('compare_reuses_local_images_after_signatures_expire_and_foreground_refreshes_metadata',async()=>{
    let requests=0,downloads=0;
    const p=page('compare',{request:async()=>{requests++;return job;},downloadJobMedia:async(id,kind)=>{downloads++;return 'wxfile://'+kind+'.jpg';}});
    p.onLoad({job:job.id,original:'expired-orig',result:'expired-result'});p.onShow();await p._refreshPromise;
    assert.equal(requests,1);assert.equal(downloads,2);assert.equal(p.data.resultUrl,'wxfile://result.jpg');
    p.onShow();await p._refreshPromise;assert.equal(requests,1);assert.equal(downloads,2);assert.equal(p.data.resultError,'');
  });
  await test('failed_result_is_visible_error_and_original_still_renders',async()=>{
    const p=page('compare',{request:async()=>job,downloadJobMedia:async(id,kind)=>{
      if(kind==='result')throw new Error('图片文件不存在');return 'wxfile://orig.jpg';}});
    p._jobId=job.id;assert.equal(await p.refreshUrls(),false);
    assert.equal(p.data.resultError,'图片文件不存在');assert.equal(p.data.originalUrl,'wxfile://orig.jpg');
    assert(fs.readFileSync(path.join(root,'miniprogram/pages/compare/compare.wxml'),'utf8').includes('onRetryMedia'));
  });
  await test('slow_original_does_not_block_result_rendering',async()=>{
    let finishOriginal;
    const p=page('compare',{request:async()=>job,downloadJobMedia:async(id,kind)=>kind==='result'?'wxfile://result.jpg':new Promise(r=>finishOriginal=r)});
    p._jobId=job.id;const pending=p.refreshUrls();await tick();assert.equal(p.data.resultUrl,'wxfile://result.jpg');
    finishOriginal('wxfile://orig.jpg');await pending;
  });
  await test('expired_original_is_not_misrepresented_as_real_before_image',async()=>{
    const p=page('compare',{request:async()=>({...job,orig_url:null}),downloadJobMedia:async()=> 'wxfile://result.jpg'});
    p._jobId=job.id;await p.refreshUrls();assert.equal(p.data.originalUnavailable,true);
    assert.equal(p.data.originalUrl,p.data.resultUrl);assert(p.data.label.includes('90 × 160'));
  });
  await test('compare_image_error_retry_is_bounded',async()=>{
    let requests=0;
    const p=page('compare',{request:async()=>{requests++;return job;},downloadJobMedia:async(id,kind)=>'wxfile://'+kind+'.jpg'});
    p._jobId=job.id;await p.refreshUrls();p.onResultError();await p._refreshPromise;
    p.onResultError();await tick();assert.equal(requests,2);assert(p.data.resultError.includes('重试'));
    p.onRetryMedia();await p._refreshPromise;assert.equal(requests,3);assert.equal(p.data.resultError,'');
  });
  await test('unloaded_compare_page_ignores_pending_requests',async()=>{
    let resolve;const p=page('compare',{request:()=>new Promise(r=>resolve=r),downloadJobMedia:async()=> 'wxfile://result.jpg'});
    p._jobId=job.id;const pending=p.refreshUrls();p.onUnload();resolve(job);await pending;assert.equal(p.data.resultUrl,'');
  });
  await test('new_pending_photo_defaults_to_light_and_light_price',async()=>{
    const p=page('adjust');assert.equal(p.data.quality,'light');assert.equal(p.data.currentQualityCost,40);
    p.data.costLight=2;p.refreshCurrentCost();assert.equal(p.data.currentQualityCost,2);
  });
  await test('style_detail_hero_uses_each_cover_natural_ratio',async()=>{
    const p=page('style-detail');
    p.onCoverLoad({detail:{width:1600,height:900},currentTarget:{dataset:{index:0}}});
    assert.equal(p.data.bannerHeight,390);
    p.onCoverLoad({detail:{width:900,height:900},currentTarget:{dataset:{index:1}}});
    assert.equal(p.data.bannerHeight,390);
    p.onBannerSwiperChange({detail:{current:1}});
    assert.equal(p.data.bannerHeight,694);
    p.onCoverLoad({detail:{width:720,height:900},currentTarget:{dataset:{index:1}}});
    assert.equal(p.data.bannerHeight,868);
    const xml=fs.readFileSync(path.join(root,'miniprogram/pages/style-detail/style-detail.wxml'),'utf8');
    assert.equal((xml.match(/mode="aspectFit"/g)||[]).length,2);
    assert(!xml.includes('mode="aspectFill"'));
    assert(xml.includes('height: {{ bannerHeight }}rpx'));
  });
  await test('style_detail_price_badge_uses_current_server_price',async()=>{
    const tpl={id:'poster',name:'复古电影海报',engine:'fine',price:0,cover:'cover.jpg'};
    const p=page('style-detail',{templates:async()=>({items:[tpl]}),config:async()=>({free_mode:false,prices:{fine:5}}),absolute:x=>x},
      {setNavigationBarTitle(){}});
    p.onLoad({id:'poster'});await tick();await tick();
    assert.equal(p.data.priceLabel,'✦ 5 光子');
    assert.equal(p.data.template.coverUrl,'cover.jpg');
  });
  await test('plain_restore_sends_custom_requirement',async()=>{
    let sent;
    const p=page('adjust',{submitJob:async(_,form)=>{sent=form;throw new Error('fixture');}});
    p._foreground=true;p.data.customPrompt='移除背景人群，保留主体';
    await p.executeUpload('photo.jpg');assert.equal(sent.custom_prompt,p.data.customPrompt);
  });
  await test('violation_dialog_routes_feedback_to_wechat_service',async()=>{
    const violation={code:'CONTENT_VIOLATION',violation_id:'event123',message:'图片内容未通过安全审核',charged:1,weekly_count:2,banned:false};
    const p=page('adjust',{submitJob:async()=>{const e=new Error(violation.message);e.detail=violation;throw e;}});
    p._foreground=true;await p.executeUpload('photo.jpg');assert.equal(p.data.showViolationNotice,true);
    const xml=fs.readFileSync(path.join(root,'miniprogram/pages/adjust/adjust.wxml'),'utf8');
    assert(xml.includes('open-type="contact"'));assert(xml.includes('session-from="violation-{{ violationId }}"'));
    p.onAcknowledgeViolation();assert.equal(p.data.showViolationNotice,false);
  });
  await test('selecting_template_resets_old_crop_and_disables_ratio_picker',async()=>{
    const p=page('adjust');p._imgWidth=160;p._imgHeight=90;
    p.onSelectRatioOption({currentTarget:{dataset:{key:'9:16',label:'9:16'}}});
    p.onSelectTemplate({currentTarget:{dataset:{template:{id:'poster',name:'长图',engine:'fine',price:4}}}});
    p.onOpenRatioModal();assert.equal(p.data.showRatioModal,false);assert.equal(p.data.currentRatioKey,'original');
    assert.equal(p.data.currentQualityCost,4);
    const xml=fs.readFileSync(path.join(root,'miniprogram/pages/adjust/adjust.wxml'),'utf8');
    assert(xml.includes('wx:if="{{ !selectedTemplate }}"'));assert(xml.includes("selectedTemplate ? 'aspectFit'"));
  });
  await test('template_payload_has_no_aspect_ratio_plain_restore_keeps_choice',async()=>{
    const submissions=[];const p=page('adjust',{submitJob:async(file,form)=>{submissions.push(form);return {code:0,job_id:job.id};},absolute:x=>x,
      waitForJob:async()=>{const e=new Error('background');e.code='USER_BACKGROUND';throw e;}});
    p._foreground=false;p.data.currentRatioKey='9:16';p.data.selectedTemplate={id:'poster',name:'长图'};
    await p.executeUpload('photo.jpg');assert.equal(submissions[0].template_id,'poster');assert.equal(submissions[0].aspect_ratio,undefined);
    p.data.selectedTemplate=null;await p.executeUpload('photo.jpg');assert.equal(submissions[1].quality,'light');assert.equal(submissions[1].aspect_ratio,'9:16');
  });
  await test('pending_upload_cannot_be_misreported_as_background_job',async()=>{
    let toast='',navigated=0;
    const p=page('adjust',{}, {showToast:o=>{toast=o.title;},navigateTo:()=>navigated++});
    p._foreground=true;p.data.processing=true;p.data.currentJobId=null;
    p.onCancelOrMinimizeWait();
    assert.equal(p.data.processing,true);assert.equal(p._foreground,true);
    assert.equal(navigated,0);assert(toast.includes('尚未提交'));
  });
  await test('uncertain_submission_redirects_to_works_instead_of_second_charge',async()=>{
    let submits=0,fileInfoCalls=0,modal;
    const api={submitJob:async()=>{submits++;const e=new Error('请求超时');e.jobSubmissionAttempted=true;throw e;}};
    const p=page('adjust',api,{getFileInfo:()=>fileInfoCalls++,showModal:o=>{modal=o;}});
    p._foreground=true;p.data.freeMode=true;p.data.imagePath='fixture.jpg';
    await p.executeUpload('fixture.jpg');assert.equal(modal.title,'提交状态待确认');
    await p.onStartGenerate();assert.equal(submits,1);assert.equal(fileInfoCalls,0);
  });
  await test('accepted_job_can_still_be_backgrounded',async()=>{
    const p=page('adjust');p._foreground=true;p.data.processing=true;p.data.currentJobId='abcdef123456';
    p.onCancelOrMinimizeWait();assert.equal(p.data.processing,false);assert.equal(p._foreground,false);
  });
  await test('saving_uses_local_result_without_network_or_waiting_for_original',async()=>{
    let saved;
    const p=page('compare',{downloadJobMedia:()=>{throw new Error('no network');}},
      {saveImageToPhotosAlbum:o=>{saved=o.filePath;o.success({});}});
    p._jobId=job.id;p._mediaCache={result:'wxfile://result.jpg'};await p.onDownload();
    assert.equal(saved,'wxfile://result.jpg');assert.equal(p.data.saving,false);
  });
  await test('missing_temp_result_redownloads_directly_once_before_album_save',async()=>{
    let downloads=0,saves=0,saved;
    const p=page('compare',{downloadJobMedia:async()=>{downloads++;return 'wxfile://fresh.jpg';}},
      {saveImageToPhotosAlbum:o=>{saves++;if(saves===1)o.fail({errMsg:'saveImageToPhotosAlbum:fail file not exist'});else{saved=o.filePath;o.success({});}}});
    p._jobId=job.id;p._mediaCache={result:'wxfile://deleted.jpg'};await p.onDownload();await tick();
    assert.equal(downloads,1);assert.equal(saved,'wxfile://fresh.jpg');assert.equal(p.data.saving,false);
  });
  await test('album_permission_error_is_not_misreported_as_cos_failure',async()=>{
    let modal;
    const p=page('compare',{}, {saveImageToPhotosAlbum:o=>o.fail({errMsg:'saveImageToPhotosAlbum:fail auth deny'}),showModal:o=>{modal=o;}});
    p._mediaCache={result:'wxfile://result.jpg'};await p.onDownload();assert.equal(modal.title,'需要相册权限');assert.equal(p.data.saving,false);
  });
  await test('album_filename_extension_retry_cleans_temporary_copy',async()=>{
    let saves=0,copied,removed;
    const p=page('compare',{}, {env:{USER_DATA_PATH:'/wxdata'},getFileSystemManager:()=>({copyFileSync:(src,dst)=>{copied=dst;},unlinkSync:p=>{removed=p;}}),
      saveImageToPhotosAlbum:o=>{saves++;saves===1?o.fail({errMsg:'invalid file suffix'}):o.success({});}});
    p._mediaCache={result:'wxfile://image.tmp'};await p.onDownload();assert(copied.endsWith('.jpg'));assert.equal(removed,copied);assert.equal(saves,2);
  });
  await test('changed_javascript_and_wxml_handlers_are_valid',()=>{
    for(const name of ['compare','adjust','style-detail']) {
      const p=page(name),xml=fs.readFileSync(path.join(root,`miniprogram/pages/${name}/${name}.wxml`),'utf8');
      for(const m of xml.matchAll(/\b(?:bind(?::)?\w+|catch(?::)?\w+)=["']([a-zA-Z_$][\w$]*)["']/g))
        if(!['true','false'].includes(m[1]))assert.equal(typeof p[m[1]],'function',m[1]);
    }
    const html=fs.readFileSync(path.join(root,'backend/admin.html'),'utf8');
    for(const m of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g))new vm.Script(m[1]);
    assert(html.includes('data-act="cleanup"'));assert(html.includes('用户再次使用会自动显示'));
  });
  const failed=rows.filter(x=>!x.passed).length;
  fs.writeFileSync(path.join(output,'workflow_frontend_results.json'),JSON.stringify({cases:rows},null,2),'utf8');
  console.log(`WORKFLOW_FRONTEND_SUMMARY total=${rows.length} passed=${rows.length-failed} failed=${failed}`);
  process.exitCode=failed?1:0;
})();
