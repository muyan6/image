/* Behavioral creation regressions: isolated wx/files/requests; no paid service. */
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const root=path.resolve(process.env.REVIEW_ROOT||path.resolve(__dirname,'../..'));
const out=path.resolve(process.env.REVIEW_OUTPUT||path.join(root,'audit/experience-creation'));
const rows=[],tick=()=>new Promise(r=>setImmediate(r));
function fixture(overrides={}){
  const storage=new Map(),files=new Set(['wxfile://photo.jpg','wxfile://other.jpg','wxfile://download.jpg']);
  const calls=[],modals=[],removed=[];let copyingFails=false;
  const app={globalData:{userId:'WX-1111111111111111',lightPoints:200,freeMode:false,historyList:[]},setBalance(n){this.globalData.lightPoints=n;},persist(){}};
  const wx={env:{USER_DATA_PATH:'/files'},getStorageSync:k=>storage.get(k),setStorageSync:(k,v)=>storage.set(k,JSON.parse(JSON.stringify(v))),removeStorageSync:k=>storage.delete(k),
    getFileSystemManager:()=>({accessSync:p=>{if(!files.has(p))throw new Error('missing');},copyFile:o=>{if(copyingFails){o.fail(new Error('full'));return;}if(!files.has(o.srcPath)){o.fail(new Error('missing'));return;}files.add(o.destPath);o.success();},unlinkSync:p=>{removed.push(p);files.delete(p);}}),
    navigateTo:o=>{calls.push({navigate:o.url});o.success&&o.success();},switchTab:o=>calls.push({tab:o.url}),redirectTo:o=>calls.push({redirect:o.url}),
    showModal:o=>modals.push(o),showToast:o=>calls.push({toast:o.title}),getImageInfo:o=>o.success({width:100,height:100}),
    getFileInfo:o=>o.success({size:4}),chooseMedia:o=>o.success({tempFiles:[{tempFilePath:'wxfile://other.jpg'}]}),vibrateShort(){},showLoading(){},hideLoading(){}};
  const api={absolute:x=>x,ensureLogin:async()=>true,config:async()=>({prices:{light:40,fine:80},free_mode:false,template_quality_options:['light','fine'],template_output_modes:['template','single'],text_generation:{ready:true,price:40}}),
    me:async()=>({balance:app.globalData.lightPoints}),templates:async()=>({items:[{id:'poster',name:'海报',text_fields:[{key:'title',default:'默认标题'}]}]}),
    request:async(url,opts)=>{calls.push({url,opts});return {code:0,job_id:'job_created',status:'processing',balance:160};},
    lookupSubmission:async()=>({state:'pending',job_id:null}),downloadRecipeOriginal:async(job,url)=>{calls.push({originalDownload:{job,url}});return 'wxfile://download.jpg';},
    submitJob:async(file,form)=>{calls.push({submit:{file,form}});return {code:0,job_id:'photo_created',orig_url:'https://cos.invalid/raw',balance:160};},
    waitForJob:async()=>({status:'succeeded',orig_url:'https://cos.invalid/raw',result_url:'https://cos.invalid/result'}),...overrides};
  const module={exports:{}};vm.runInNewContext(fs.readFileSync(path.join(root,'miniprogram/utils/creation-draft.js'),'utf8'),{module,require:()=>api,getApp:()=>app,wx,Date,Math,Promise,console});
  const creation=module.exports;
  function page(name){let p;vm.runInNewContext(fs.readFileSync(path.join(root,`miniprogram/pages/${name}/${name}.js`),'utf8'),{require:n=>n.includes('creation-draft')?creation:api,getApp:()=>app,wx,Page:x=>p=x,Date,Math,Promise,setTimeout,clearTimeout,console:{log(){},warn(){},error(){}}});
    p.data=JSON.parse(JSON.stringify(p.data));p.setData=patch=>{for(const [k,v]of Object.entries(patch)){const keys=k.split('.');let obj=p.data;keys.slice(0,-1).forEach(key=>obj=obj[key]||(obj[key]={}));obj[keys.at(-1)]=v;}};return p;}
  return {api,app,wx,creation,page,storage,files,calls,modals,removed,failCopies:()=>copyingFails=true};
}
async function test(name,fn){try{await fn();rows.push({case:name,passed:true});console.log('PASS '+name);}catch(e){rows.push({case:name,passed:false,error:String(e.stack)});console.error('FAIL '+name,e.message);}}
(async()=>{
  await test('text_draft_preserves_description_and_ratio_after_page_unload',async()=>{
    const f=fixture(),p=f.page('text-generation');p.onInput({detail:{value:'雨后的小屋'}});p.onRatio({currentTarget:{dataset:{key:'2:3'}}});p.onUnload();await tick();
    const next=f.page('text-generation');next.onLoad({restoreDraft:1});assert.equal(next.data.prompt,'雨后的小屋');assert.equal(next.data.ratio,'2:3');
  });
  await test('photo_draft_has_one_durable_image_and_keeps_all_creation_fields_not_rights',async()=>{
    const f=fixture();const value={input_mode:'photo',imagePath:'wxfile://photo.jpg',quality:'fine',template_id:'poster',text_fields:{title:'自定义'},custom_prompt:'柔和',show_custom_prompt:true,aspect_ratio:'original',template_output_mode:'single',rightsOk:true};
    const first=await f.creation.saveDraft(value);assert(first.imagePath.startsWith('/files/creation_draft_'));assert(!('rightsOk'in first));
    await f.creation.saveDraft({...value,text_fields:{title:'更改'}});await f.creation.saveDraft({...value,text_fields:{title:'再次更改'}});
    assert.equal(f.files.size,4);assert.equal(f.creation.readDraft().imagePath,first.imagePath);assert.equal(f.creation.readDraft().text_fields.title,'再次更改');
  });
  await test('new_photo_replaces_only_owned_previous_draft_image',async()=>{
    const f=fixture(),first=await f.creation.saveDraft({input_mode:'photo',imagePath:'wxfile://photo.jpg'});const next=await f.creation.saveDraft({input_mode:'photo',imagePath:'wxfile://other.jpg'});
    assert(!f.files.has(first.imagePath));assert(f.files.has(next.imagePath));assert(f.files.has('wxfile://photo.jpg'));assert.equal(f.removed.length,1);
  });
  await test('owned_file_cleanup_rejects_cached_traversal_path',async()=>{
    const f=fixture();f.storage.set('creationDraftV1',{owner:f.app.globalData.userId,input_mode:'photo',imagePath:'/files/creation_draft_1_abc.jpg/../../private.jpg'});
    await f.creation.clearDraft();assert.equal(f.removed.length,0);
  });
  await test('copy_failure_retains_parameters_and_explicit_missing_photo',async()=>{
    const f=fixture();f.failCopies();const saved=await f.creation.saveDraft({input_mode:'photo',imagePath:'wxfile://photo.jpg',text_fields:{title:'保留'}});
    assert(saved.imageMissing);assert.equal(saved.imagePath,'');assert.equal(saved.text_fields.title,'保留');await f.creation.resumeDraft();assert(f.calls.at(-1).navigate.includes('restoreDraft=1'));
  });
  await test('different_account_cannot_read_anonymous_or_other_user_draft',async()=>{
    const f=fixture();await f.creation.saveDraft({input_mode:'text',prompt:'私有'});f.app.globalData.userId='WX-2222222222222222';assert.equal(f.creation.readDraft(),null);
    f.storage.set('creationDraftV1',{owner:'',input_mode:'text',prompt:'匿名'});assert.equal(f.creation.readDraft(),null);
  });
  await test('draft_loaded_before_first_login_is_rebound_only_by_its_active_page_save',async()=>{
    const f=fixture();f.app.globalData.userId='';await f.creation.saveDraft({input_mode:'text',prompt:'尚未登录的描述',aspect_ratio:'1:1'});
    const p=f.page('text-generation');p.onLoad({restoreDraft:1});f.app.globalData.userId='WX-1111111111111111';assert.equal(f.creation.readDraft(),null);await p.saveDraft();assert.equal(f.creation.readDraft().prompt,'尚未登录的描述');
  });
  await test('restored_photo_requires_reconfirmation_and_keeps_fields',async()=>{
    const f=fixture();await f.creation.saveDraft({input_mode:'photo',imagePath:'wxfile://photo.jpg',quality:'fine',template_id:'poster',text_fields:{title:'我的标题'},custom_prompt:'柔和',show_custom_prompt:true,template_output_mode:'single'});
    const p=f.page('adjust');p.onLoad({restoreDraft:1});await tick();assert.equal(p.data.rightsOk,false);assert.equal(p.data.quality,'fine');assert.equal(p.data.textValues.title,'我的标题');assert.equal(p.data.templateOutputMode,'single');
    await p.onStartGenerate();assert(!f.calls.some(c=>c.submit));
  });
  await test('missing_photo_can_be_reselected_without_losing_recipe',async()=>{
    const f=fixture();f.failCopies();await f.creation.saveDraft({input_mode:'photo',imagePath:'wxfile://photo.jpg',quality:'fine',template_id:'poster',text_fields:{title:'保留'}});
    const p=f.page('adjust');p.onLoad({restoreDraft:1});await tick();p.onChooseDraftPhoto();assert.equal(p.data.imagePath,'wxfile://other.jpg');assert.equal(p.data.textValues.title,'保留');assert.equal(p.data.rightsOk,false);
  });
  await test('missing_template_detail_never_substitutes_first_template',async()=>{
    const f=fixture(),p=f.page('style-detail');await p.loadTemplateDetail('deleted');assert.equal(p.data.template,null);assert(p.data.loadError.includes('下架'));p.onChoosePhoto();assert(!f.calls.some(c=>c.navigate));
  });
  await test('template_network_error_blocks_target_generation_and_retry_recovers',async()=>{
    let failed=true;const f=fixture({templates:async()=>{if(failed)throw new Error('offline');return {items:[{id:'poster',name:'海报'}]};}}),p=f.page('adjust');p.data.imagePath='wxfile://photo.jpg';p.data.priceReady=true;p.data.lightPoints=200;
    await p.loadTemplatesData('poster');assert(!p.data.templateReady);await p.onStartGenerate();assert(!f.calls.some(c=>c.submit));failed=false;await p.loadTemplatesData('poster');assert(p.data.templateReady);assert.equal(p.data.selectedTemplate.id,'poster');
  });
  await test('removed_template_stays_unconfirmed_until_explicit_other_choice',async()=>{
    const f=fixture(),p=f.page('adjust');p.data.templateId='deleted';await p.loadTemplatesData('deleted');assert.equal(p.data.templateId,'deleted');assert(!p.data.templateReady);
    p.onSelectTemplate({currentTarget:{dataset:{template:{id:'',name:'原片修复'}}}});assert(p.data.templateReady);assert.equal(p.data.templateId,'');
  });
  await test('text_shows_server_balance_and_submits_displayed_expected_price',async()=>{
    const f=fixture(),p=f.page('text-generation');await p.onShow();p.onInput({detail:{value:'森林小屋'}});await p.onGenerate();const post=f.calls.find(c=>c.url==='/api/text-generation');
    assert.equal(post.opts.data.expected_price,40);assert(post.opts.data.client_request_id);assert.equal(p.data.lightPoints,160);assert.equal(f.creation.readPendingSubmission(),null);
  });
  await test('text_insufficient_balance_does_not_post_and_links_to_supply',async()=>{
    const f=fixture(),p=f.page('text-generation');await p.onShow();p.data.lightPoints=10;p.data.prompt='小屋';await p.onGenerate();assert(!f.calls.some(c=>c.url==='/api/text-generation'));assert(f.modals.at(-1).content.includes('40'));f.modals.at(-1).success({confirm:true});assert(f.calls.at(-1).navigate.includes('/credits/'));
  });
  await test('text_price_change_clears_definite_submission_and_refreshes_price',async()=>{
    const f=fixture(),p=f.page('text-generation');await p.onShow();p.data.prompt='小屋';f.api.config=async()=>({text_generation:{ready:true,price:80},free_mode:false});f.api.request=async()=>{throw Object.assign(new Error('价格已更新'),{status:409});};
    await p.onGenerate();await tick();assert.equal(p.data.price,80);assert.equal(f.creation.readPendingSubmission(),null);assert(!p.data.submissionPending);
  });
  await test('text_uncertain_post_is_persistent_and_never_auto_reposts',async()=>{
    let posts=0;const f=fixture({request:async()=>{posts++;throw Object.assign(new Error('timeout'),{jobSubmissionAttempted:true});}}),p=f.page('text-generation');await p.onShow();p.data.prompt='小屋';await p.onGenerate();await tick();const id=f.creation.readPendingSubmission().client_request_id;
    await p.onGenerate();assert.equal(posts,1);p.onUnload();const next=f.page('text-generation');next.onLoad({});await next.onShow();await tick();assert(next.data.submissionPending);assert.equal(f.creation.readPendingSubmission().client_request_id,id);assert.equal(posts,1);
  });
  await test('accepted_submission_lookup_finds_exact_job_and_preserves_balance',async()=>{
    const f=fixture();const pending=f.creation.beginSubmission('text',{prompt:'小屋',aspect_ratio:'1:1',expected_price:40});f.api.lookupSubmission=async id=>{assert.equal(id,pending.client_request_id);return {state:'accepted',job_id:'found_exact',status:'processing',balance:160};};
    const p=f.page('text-generation');await p.onConfirmSubmission();assert.equal(f.app.globalData.historyList[0].jobId,'found_exact');assert.equal(f.app.globalData.lightPoints,160);assert.equal(f.creation.readPendingSubmission(),null);assert(f.calls.at(-1).navigate.includes('/works/'));
  });
  await test('not_found_allows_only_original_request_id_on_manual_retry',async()=>{
    const f=fixture(),pending=f.creation.beginSubmission('text',{prompt:'原始描述',aspect_ratio:'1:1',expected_price:40});f.api.lookupSubmission=async()=>{throw Object.assign(new Error('未见登记'),{status:404});};const p=f.page('text-generation');p.data.prompt='原始描述';await p.onShow();await tick();assert(p.data.retryableSubmission);
    p.onInput({detail:{value:'偷偷更改'}});assert.equal(p.data.prompt,'原始描述');await p.onGenerate();const post=f.calls.find(c=>c.url==='/api/text-generation');assert.equal(post.opts.data.client_request_id,pending.client_request_id);assert.equal(post.opts.data.prompt,'原始描述');
  });
  await test('lookup_network_error_is_not_interpreted_as_unaccepted',async()=>{
    const f=fixture();f.creation.beginSubmission('text',{prompt:'小屋',aspect_ratio:'1:1'});f.api.lookupSubmission=async()=>{throw new Error('offline');};const p=f.page('text-generation');await p.onConfirmSubmission();assert(p.data.submissionPending);assert(!p.data.retryableSubmission);assert(f.creation.readPendingSubmission());
  });
  await test('confirmed_rejected_submission_unlocks_inputs_without_charging',async()=>{
    const f=fixture();f.creation.beginSubmission('text',{prompt:'小屋',aspect_ratio:'1:1'});f.api.lookupSubmission=async()=>({state:'rejected',detail:'未受理'});const p=f.page('text-generation');await p.onConfirmSubmission();assert(!p.data.submissionPending);assert.equal(f.creation.readPendingSubmission(),null);assert(!f.calls.some(c=>c.url==='/api/text-generation'));
  });
  await test('photo_submit_has_persisted_id_and_recent_template_only_after_acceptance',async()=>{
    const f=fixture(),p=f.page('adjust');p.data.imagePath='wxfile://photo.jpg';p.data.selectedTemplate={id:'poster',name:'海报'};p.data.templateId='poster';p._foreground=true;p.data.processing=true;
    f.api.submitJob=async(file,form)=>{assert.equal(f.creation.readPendingSubmission().client_request_id,form.client_request_id);f.calls.push({submit:{file,form}});return {code:0,job_id:'photo_created',balance:160};};await p.executeUpload('wxfile://photo.jpg');
    assert(f.calls.some(c=>c.url==='/api/me/recent-template'));assert.equal(f.creation.readPendingSubmission(),null);
  });
  await test('photo_uncertain_submit_does_not_record_recent_or_retry_paid_post',async()=>{
    const f=fixture({submitJob:async()=>{throw Object.assign(new Error('timeout'),{jobSubmissionAttempted:true});}}),p=f.page('adjust');p.data.imagePath='wxfile://photo.jpg';p.data.templateId='poster';p.data.selectedTemplate={id:'poster'};p._foreground=true;p.data.processing=true;await p.executeUpload('wxfile://photo.jpg');
    assert(p.data.submissionPending);assert(f.creation.readPendingSubmission());assert(!f.calls.some(c=>c.url==='/api/me/recent-template'));assert(!f.calls.some(c=>c.submit));
  });
  await test('recreation_uses_complete_recipe_and_full_original_not_comparison',async()=>{
    const recipe={recipe_complete:true,input_mode:'photo',quality:'fine',template_id:'poster',text_fields:{title:'沿用'},custom_prompt:'柔光',aspect_ratio:'original',template_output_mode:'single',orig_url:'https://cos.invalid/full-original'};
    const f=fixture({request:async()=>recipe});await f.creation.recreateFromJob('old_job');const draft=f.creation.readDraft();assert.equal(draft.quality,'fine');assert.equal(draft.text_fields.title,'沿用');assert.equal(draft.template_output_mode,'single');assert.equal(f.calls.find(c=>c.originalDownload).originalDownload.url,recipe.orig_url);assert(f.calls.at(-1).navigate.includes('/adjust/'));
  });
  await test('legacy_incomplete_recipe_and_expired_original_do_not_fabricate_parameters',async()=>{
    const f=fixture({request:async()=>({recipe_complete:false,input_mode:'text',missing_fields:['prompt']})});await assert.rejects(f.creation.recreateFromJob('old'),/参数不完整/);assert.equal(f.creation.readDraft(),null);
    f.api.request=async()=>({recipe_complete:true,input_mode:'photo',orig_url:''});await assert.rejects(f.creation.recreateFromJob('expired'),/保存期限/);assert(!f.calls.some(c=>c.navigate));
  });
  await test('complete_text_recipe_restores_prompt_without_image_download',async()=>{
    const f=fixture({request:async()=>({recipe_complete:true,input_mode:'text',prompt:'沿用描述',aspect_ratio:'3:2'})});await f.creation.recreateFromJob('text_job');assert.equal(f.creation.readDraft().prompt,'沿用描述');assert.equal(f.creation.readDraft().aspect_ratio,'3:2');assert(!f.calls.some(c=>c.originalDownload));
  });
  await test('pending_submission_prevents_cross_mode_draft_replacement_and_recreation',async()=>{
    const f=fixture();await f.creation.saveDraft({input_mode:'photo',imagePath:'wxfile://photo.jpg'});const image=f.creation.readDraft().imagePath;f.creation.beginSubmission('photo',{draft:f.creation.readDraft()});
    await assert.rejects(f.creation.saveDraft({input_mode:'text',prompt:'另一个描述'}),/待确认/);await assert.rejects(f.creation.recreateFromJob('other'),/待确认/);assert(f.files.has(image));
  });
  await test('photo_retry_after_missing_local_image_keeps_original_submission_number',async()=>{
    const f=fixture();const saved=await f.creation.saveDraft({input_mode:'photo',imagePath:'wxfile://photo.jpg',template_id:'poster',quality:'light',text_fields:{title:'冻结'},aspect_ratio:'original'});
    f.api.submitJob=async(file,form)=>{assert(f.files.has(file),'重新选择的原图须在提交时存在');f.calls.push({submit:{file,form}});return {code:0,job_id:'photo_created'};};
    const pending=f.creation.beginSubmission('photo',{form:{quality:'light',template_id:'poster',expected_price:40,text_fields:JSON.stringify(saved.text_fields)},draft:saved});
    f.files.delete(saved.imagePath);f.api.lookupSubmission=async()=>{throw Object.assign(new Error('登记不存在'),{status:404});};
    const p=f.page('adjust');p.onLoad({restoreDraft:1});p.onShow();await tick();assert(p.data.imageMissing);assert(p.data.retryableSubmission);
    p.onChooseDraftPhoto();await tick();p.onToggleRights();await p.onStartGenerate();await tick();
    const submit=f.calls.find(c=>c.submit).submit;assert.equal(submit.form.client_request_id,pending.client_request_id);assert.equal(submit.form.text_fields,JSON.stringify({title:'冻结'}));assert.notEqual(submit.file,saved.imagePath);
  });
  await test('retry_preflight_error_never_discards_old_uncertain_id',async()=>{
    const f=fixture({submitJob:async()=>{throw Object.assign(new Error('配置价格已更新'),{status:409});}});const saved=await f.creation.saveDraft({input_mode:'photo',imagePath:'wxfile://photo.jpg',quality:'light'});
    const pending=f.creation.beginSubmission('photo',{form:{quality:'light',expected_price:40},draft:saved});f.api.lookupSubmission=async()=>{throw Object.assign(new Error('登记不存在'),{status:404});};
    const p=f.page('adjust');p.onLoad({restoreDraft:1});p.onShow();await tick();await p.executeUpload(saved.imagePath);
    assert.equal(f.creation.readPendingSubmission().client_request_id,pending.client_request_id);assert(p.data.submissionPending);assert(p.data.retryableSubmission);
  });
  await test('stale_template_response_does_not_undo_explicit_user_selection',async()=>{
    let finish;const f=fixture({templates:()=>new Promise(r=>finish=r)}),p=f.page('adjust');p.data.templateId='poster';const loading=p.loadTemplatesData('poster');
    p.onSelectTemplate({currentTarget:{dataset:{template:{id:'',name:'原片修复'}}}});finish({items:[{id:'poster',name:'海报'}]});await loading;assert.equal(p.data.templateId,'');assert(p.data.templateReady);
  });
  await test('account_changed_during_recipe_read_never_saves_other_account_parameters',async()=>{
    let finish;const f=fixture({request:()=>new Promise(r=>finish=r)});const loading=f.creation.recreateFromJob('old');await tick();f.app.globalData.userId='WX-2222222222222222';finish({recipe_complete:true,input_mode:'text',prompt:'旧账户描述'});
    await assert.rejects(loading,/登录状态已变化/);assert.equal(f.creation.readDraft(),null);assert(!f.calls.some(c=>c.navigate));
  });
  fs.mkdirSync(out,{recursive:true});fs.writeFileSync(path.join(out,'creation_results.json'),JSON.stringify({cases:rows},null,2));
  const failed=rows.filter(r=>!r.passed).length;console.log(`CREATION_SUMMARY total=${rows.length} passed=${rows.length-failed} failed=${failed}`);process.exitCode=failed?1:0;
})();
