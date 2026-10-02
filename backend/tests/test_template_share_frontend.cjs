/* Execute the real mini-program sorting/forms/helpers against isolated wx/API doubles. */
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const root=path.resolve(process.env.REVIEW_ROOT||path.resolve(__dirname,'../..')),out=path.resolve(process.env.REVIEW_OUTPUT||path.join(root,'audit/template-sharing-tests'));
const cases=[],tick=()=>new Promise(r=>setImmediate(r));
function helper(api,wx={}){const module={exports:{}};vm.runInNewContext(fs.readFileSync(path.join(root,'miniprogram/utils/template-sharing.js'),'utf8'),{module,require:()=>api,wx,console,Uint8Array,Date,Math});return module.exports;}
function page(name,api={},wx={},app={globalData:{}}){let value;const share=helper(api,wx);vm.runInNewContext(fs.readFileSync(path.join(root,`miniprogram/pages/${name}/${name}.js`),'utf8'),{Page:v=>value=v,getApp:()=>app,require:n=>n.includes('template-sharing.js')?share:api,wx:{showToast(){},...wx},console,setTimeout,clearTimeout,Date,Math});value.data=JSON.parse(JSON.stringify(value.data));value.setData=function(p){Object.assign(this.data,p)};return value;}
async function test(name,fn){try{await fn();cases.push({case:name,passed:true});console.log('PASS '+name)}catch(error){cases.push({case:name,passed:false,error:error.stack});console.error('FAIL '+name,error)}}
(async()=>{
 await test('all_defaults_hot_both_origins_and_regular_categories_use_editorial_order',()=>{
  const p=page('templates',{absolute:x=>x});p._renderData([{id:'g',name:'分类'}],[{id:'official',group_id:'g',source:'official',sort:1,usage_count:2},{id:'user',group_id:'',source:'user',usage_count:10},{id:'high',group_id:'g',sort:2,usage_count:9}]);
  assert.equal(p.data.sortMode,'hot');assert.equal(p.data.filteredTemplates.map(x=>x.id).join(','),'user,high,official');assert.equal(p.data.allTemplates.find(x=>x.id==='user').sourceLabel,'用户分享');p.filterByCategory('g');assert.equal(p.data.filteredTemplates.map(x=>x.id).join(','),'official,high');assert(!p.data.categories.some(x=>x.id==='user'||x.id==='latest'));
 });
 await test('latest_uses_first_publication_not_edit_time_and_search_combines_sort',()=>{
  const p=page('templates',{absolute:x=>x});p._renderData([],[{id:'old',name:'模板旧',published_at:1,updated_at:500,usage_count:9},{id:'new',name:'模板新',published_at:100,usage_count:0}]);p.onSortMode({currentTarget:{dataset:{id:'latest'}}});assert.equal(p.data.filteredTemplates[0].id,'new');p.onSearchInput({detail:{value:'旧'}});assert.equal(p.data.filteredTemplates[0].id,'old');assert.equal(p.data.filteredTemplates.length,1);
 });
 await test('random_is_stable_until_explicit_shuffle_and_never_rewrites_counts',()=>{
  const p=page('templates',{absolute:x=>x});p._renderData([],[1,2,3,4,5].map(n=>({id:'t'+n,usage_count:n,name:'模板'})));p.onSortMode({currentTarget:{dataset:{id:'random'}}});const ranks=p._randomRanks,order=p.data.filteredTemplates.map(x=>x.id).join();p.filterByCategory('all');assert.equal(p.data.filteredTemplates.map(x=>x.id).join(),order);assert.strictEqual(p._randomRanks,ranks);p.onShuffleTemplates();assert.notStrictEqual(p._randomRanks,ranks);assert.equal(p.data.allTemplates.reduce((s,x)=>s+x.usage_count,0),15);
 });
 await test('content_payload_contains_consent_and_no_price_model_owner_or_source',()=>{
  const h=helper({}),body=h.content({name:'分享',subtitle:'说明',prompt:'保留主体',advice:'清晰照片',suitable:'人像\n风景',engine:'free',price:0,author_openid:'spoof'},['a'],true);assert.equal(body.consent,true);assert.equal(body.submit,true);assert.equal(body.guide.suitable.length,2);for(const k of ['engine','price','author_openid','source','model_override','group_id'])assert(!(k in body));assert.throws(()=>h.content({name:'x'.repeat(21)},[],false));
 });
 await test('ai_guide_import_is_narrow_validated_and_does_not_modify_prompt',()=>{
  const h=helper({}),g=h.importGuide('```json\n{"advice":"清晰主体","suitable":["人像"],"tips":[]}\n```');assert.equal(g.suitable,'人像');assert.throws(()=>h.importGuide('{"advice":"ok","model":"free"}'));assert.throws(()=>h.importGuide('{"suitable":[4]}'));assert.throws(()=>h.guideFrom({tips:'x'.repeat(61)}));assert(h.guidePrompt({name:'风景',prompt:'watercolor'}).includes('watercolor'));
 });
 await test('actual_signed_put_is_direct_without_auth_and_uses_ticket_headers',async()=>{
  let observed;const h=helper({isJobCosUrl:url=>url.startsWith('https://cos.invalid')},{request:o=>{observed=o;o.success({statusCode:200})}});await h.signedPut({url:'https://cos.invalid/cover?signature=fixture',headers:{'Content-Type':'image/jpeg','x-cos-acl':'private'}},{buffer:new Uint8Array([1]).buffer,content_type:'image/jpeg'});assert.equal(observed.method,'PUT');assert.equal(observed.header['content-type'],'image/jpeg');assert.equal(observed.header['Content-Type'],undefined);assert.equal(observed.header.Authorization,undefined);assert.equal(observed.header.Cookie,undefined);assert.equal(observed.header['x-cos-acl'],'private');await assert.rejects(h.signedPut({url:'http://api.invalid/cover'},{buffer:[],content_type:'image/jpeg'}));await assert.rejects(h.signedPut({url:'https://cos.invalid/cover',headers:{Authorization:'no'}},{buffer:[],content_type:'image/jpeg'}));
 });
 await test('read_image_checks_magic_size_and_rejects_non_images_before_ticket',async()=>{
  const bytes=new Uint8Array([255,216,255,1]),h=helper({},{getFileSystemManager:()=>({readFile:o=>o.success({data:bytes.buffer})})});const d=await h.readImage('wxfile://fixture');assert.equal(d.ext,'jpg');assert.equal(d.size,4);const bad=helper({},{getFileSystemManager:()=>({readFile:o=>o.success({data:new Uint8Array([1,2,3]).buffer})})});await assert.rejects(bad.readImage('fixture'));
 });
 await test('submit_requires_author_consent_and_repeat_click_does_not_repeat_post',async()=>{
  let posts=0,resolve;const api={ensureLogin:async()=>{},absolute:x=>x,request:()=>{posts++;return new Promise(r=>resolve=r)}};const p=page('template-share',api);p.onLoad({});p.data.name='模板';p.data.prompt='效果';p._tokens=['a'];await p.onSubmit();assert.equal(posts,0);p.data.consent=true;const pending=p.onSubmit();await tick();await p.onSubmit();assert.equal(posts,1);resolve({submission:{id:'ts_fixture',revision:1,status:'pending',name:'模板',prompt:'效果',guide:{},covers:['https://cos.invalid/a'],cover_tokens:['a']}});await pending;assert.equal(p.data.locked,true);assert.equal(p.data.status,'pending');
 });
 await test('draft_retry_uses_same_request_id_then_switches_to_revision_update',async()=>{
  const seen=[],api={ensureLogin:async()=>{},absolute:x=>x,request:async(url,o)=>{seen.push({url,...o});return {submission:{id:'ts_fixture',revision:seen.length,status:'draft',guide:{},covers:[],cover_tokens:[]}}}};const p=page('template-share',api);p.onLoad({});p.data.name='草稿';await p.onSaveDraft();await p.onSaveDraft();assert.equal(seen[0].method,'POST');assert.equal(seen[1].method,'PUT');assert.equal(seen[1].data.revision,1);assert.equal(seen[0].data.request_id,p._requestId);
 });
 await test('unloaded_form_does_not_apply_late_private_response',async()=>{
  let resolve;const p=page('template-share',{ensureLogin:async()=>{},request:()=>new Promise(r=>resolve=r)});p.data.shareId='ts_fixture';const pending=p.loadShare();await tick();p.onUnload();resolve({submission:{id:'ts_fixture',name:'private',revision:2,guide:{},covers:[]}});await pending;assert.notEqual(p.data.name,'private');
 });
 await test('account_switch_discards_private_prompt_cover_and_late_list',async()=>{
  let resolve;const app={globalData:{userId:'A'}},p=page('template-share',{ensureLogin:async()=>{},request:()=>new Promise(r=>resolve=r)}, {},app);p.data.shareId='ts_fixture';const pending=p.loadShare();await tick();app.globalData.userId='B';resolve({submission:{id:'ts_fixture',name:'private',prompt:'private prompt',revision:2,guide:{},covers:[{token:'a',preview_url:'https://cos.invalid/a'}],cover_tokens:['a']}});await pending;assert.equal(p.data.prompt,'');assert.equal(p.data.covers.length,0);assert.equal(p.data.locked,true);
  const mine=page('template-shares',{ensureLogin:async()=>{},request:()=>new Promise(r=>resolve=r)}, {},app);const list=mine.loadMine();await tick();app.globalData.userId='C';resolve({items:[{id:'secret',name:'private'}],rewards:{reward_credited:99}});await list;assert.equal(mine.data.items.length,0);assert.equal(mine.data.rewards,null);
 });
 await test('real_owner_cover_object_dto_prefers_thumbnail_and_keeps_tokens',()=>{
  const p=page('template-share',{absolute:x=>x});p._tokens=[];p.applyShare({id:'ts_fixture',revision:3,status:'draft',guide:{},cover_tokens:['a'],covers:[{token:'a',preview_url:'https://cos.invalid/hd',thumbnail_url:'https://cos.invalid/small'}]});assert.equal(p.data.covers[0].url,'https://cos.invalid/small');assert.equal(p._tokens[0],'a');
 });
 await test('my_templates_paginates_deduplicates_shows_author_rewards_and_old_public_revision',async()=>{
  let n=0;const p=page('template-shares',{ensureLogin:async()=>{},absolute:x=>x,request:async()=>({items:[{id:'a',revision:2,status:'draft',has_published:true,covers:['https://cos.invalid/cover']},...(n++?[{id:'b',revision:1,status:'pending',covers:[]}]:[])],rewards:{reward_earned:40,reward_credited:40,recovery_due:0},has_more:true,next_offset:24})});await p.loadMine();await p.loadMine(true);assert.equal(p.data.items.length,2);assert.equal(p.data.items[0].has_published,true);assert.equal(p.data.rewards.reward_credited,40);assert.equal(p.data.items[1].statusLabel,'审核中');
 });
 await test('template_deep_link_share_keeps_template_id_and_guide_does_not_guess_user_portrait',async()=>{
  const p=page('style-detail',{templates:async()=>({items:[{id:'user_tpl',source:'user',name:'风景',guide:{},cover:'https://cos.invalid/cover'}]}),absolute:x=>x},{setNavigationBarTitle(){}});p.data.templateId='user_tpl';await p.loadTemplateDetail('user_tpl');assert.equal(p.data.suitableList.length,0);const share=p.onShareAppMessage();assert(share.path.includes('id=user_tpl'));assert.equal(share.imageUrl,'https://cos.invalid/cover');
 });
 fs.mkdirSync(out,{recursive:true});fs.writeFileSync(path.join(out,'template_share_frontend_results.json'),JSON.stringify({cases},null,2),'utf8');const passed=cases.filter(x=>x.passed).length;console.log(`TEMPLATE_SHARE_FRONTEND_SUMMARY total=${cases.length} passed=${passed} failed=${cases.length-passed}`);process.exitCode=passed===cases.length?0:1;
})().catch(e=>{console.error(e);process.exitCode=1});
