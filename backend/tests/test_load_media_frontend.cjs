/* Offline small-media/HD isolation plus actual variant cache tests. */
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const root=path.resolve(process.env.REVIEW_ROOT||path.resolve(__dirname,'../..'));
const out=path.resolve(process.env.REVIEW_OUTPUT||path.join(root,'audit/load-media-tests')),cases=[];
const tick=()=>new Promise(r=>setImmediate(r)),expiry=Math.floor(Date.now()/1000)+3600;
const signed=(id,signature='one',small=false)=>`https://cos.invalid/${id}.jpg?q-sign-time=1%3B${expiry}&q-signature=${signature}${small?'&imageMogr2%2Fthumbnail%2F480x480%3E%2Fformat%2Fjpg%2Fquality%2F70%2Fstrip=':''}`;
function app(){return {globalData:{apiBase:'https://api.invalid',historyList:[],mediaCache:{},lightPoints:100,freeMode:false},persist(){},setBalance(n){this.globalData.lightPoints=n;}};}
function apiModule(a=app(),deferred=false){
 const module={exports:{}},valid=new Set(),pending=[];let downloads=0;
 const wx={getFileSystemManager:()=>({accessSync(p){if(!valid.has(p))throw Error('missing');}}),getImageInfo(o){downloads++;const path='wxfile://tmp/'+downloads+'.jpg';valid.add(path);if(deferred)pending.push(()=>o.success({path}));else o.success({path});}};
 vm.runInNewContext(fs.readFileSync(path.join(root,'miniprogram/utils/api.js'),'utf8'),{module,wx,getApp:()=>a,setTimeout,console});
 return {api:module.exports,a,pending,downloads:()=>downloads};
}
function page(name,api,a){
 let p;vm.runInNewContext(fs.readFileSync(path.join(root,`miniprogram/pages/${name}/${name}.js`),'utf8'),{Page:x=>p=x,getApp:()=>a,require:()=>api,console,setTimeout,clearTimeout,wx:{getStorageSync(){},setStorageSync(){},getWindowInfo:()=>({windowWidth:393}),showToast(){},stopPullDownRefresh(){}}});
 p.data=JSON.parse(JSON.stringify(p.data));p.setData=d=>Object.assign(p.data,d);return p;
}
async function test(name,fn){try{await fn();cases.push({case:name,passed:true});console.log('PASS '+name);}catch(e){cases.push({case:name,passed:false,error:e.stack});console.error(name,e);}}
(async()=>{
 const probe=apiModule();if(typeof probe.api.jobPreviewUrl!=='function'){console.log('LOAD_MEDIA_FRONTEND_BASELINE thumb_fields=not_consumed preview=hd');return;}
 await test('actual_cache_keeps_thumbnail_and_hd_variants_separate_with_rotating_signatures',async()=>{
  const t=apiModule(),hd=signed('job'),small=signed('job','one',true);
  const a=await t.api.rememberCommunityImage(small),b=await t.api.rememberCommunityImage(hd);
  assert.notEqual(a,b);assert.equal(t.downloads(),2);
  assert.equal(await t.api.rememberCommunityImage(signed('job','two',true)),a);assert.equal(t.downloads(),2);
  assert.equal(t.a.globalData.mediaCache.job,undefined);
 });
 await test('account_cache_reset_does_not_share_inflight_image_tasks_or_paths',async()=>{
  const t=apiModule(app(),true),url=signed('shared','one',true);
  const first=t.api.rememberCommunityImage(url);t.a.globalData.mediaCache={};const second=t.api.rememberCommunityImage(url);
  assert.equal(t.downloads(),2);t.pending[0]();t.pending[1]();const [a,b]=await Promise.all([first,second]);
  assert.notEqual(a,b);assert.equal(t.api.communityImage(url),b);
 });
 await test('works_small_preview_keeps_result_original_and_downloads_hd',async()=>{
  const t=apiModule(),hd=signed('work'),orig=signed('original'),small=signed('work','one',true);
  const p=page('works',t.api,t.a),w=p.mapCloudWork({id:'work',status:'succeeded',result_url:hd,orig_url:orig,thumb_url:small});
  assert.equal(w.preview,small);assert.equal(w.result,hd);assert.equal(w.original,orig);assert.equal(p.safePreview(w),small);
  const newer=p.mapCloudWork({id:'work',status:'succeeded',result_url:signed('work','two'),orig_url:orig,thumb_url:signed('work','two',true)},w);
  assert.equal(newer.preview,small);assert.equal(newer.result,hd);
 });
 await test('works_old_backend_without_thumbnail_retains_hd_compatibility',()=>{
  const t=apiModule(),p=page('works',t.api,t.a),hd=signed('legacy');
  assert.equal(p.mapCloudWork({id:'legacy',status:'succeeded',result_url:hd}).preview,hd);
 });
 await test('works_thumbnail_error_repairs_original_without_poisoning_hd_cache',async()=>{
  const t=apiModule(),hd=signed('work','repaired'),small=signed('work','one',true);
  const p=page('works',{...t.api,repairJobMedia:async()=>hd},t.a);
  p.data.works=[p.mapCloudWork({id:'work',status:'succeeded',result_url:signed('work'),thumb_url:small})];
  const originalLocal=await t.api.rememberCommunityImage(hd);t.a.globalData.mediaCache.work={result:originalLocal};
  p.onWorkImageError({currentTarget:{dataset:{index:0}}});await tick();assert.equal(p.data.works[0].preview,hd);assert.equal(p.data.works[0].thumbnail,'');assert.equal(t.a.globalData.mediaCache.work.result,originalLocal);
 });
 await test('profile_small_preview_preserves_full_result',async()=>{
  const t=apiModule(),hd=signed('my'),small=signed('my','one',true);
  const p=page('my',{...t.api,config:async()=>({}),me:async()=>({balance:100}),myJobs:async()=>({jobs:[{id:'my',status:'succeeded',result_url:hd,thumb_url:small}]})},t.a);
  await p.refreshUserData(true);assert.equal(p.data.previewWorks[0].preview,small);assert.equal(p.data.historyList[0].result,hd);
 });
 await test('community_card_uses_small_variant_while_share_and_detail_keep_hd',async()=>{
  const t=apiModule(),hd=signed('post'),small=signed('post','one',true);
  const p=page('community',{...t.api,request:async()=>({enabled:true,items:[{id:'post',resultUrl:hd,thumbnailUrl:small,origUrl:'',likes:0}]})},t.a);
  await p.loadCommunity(true);assert.equal(p.data.items[0].thumbnailUrl,small);assert.equal(p.data.items[0].resultUrl,hd);
  assert(fs.readFileSync(path.join(root,'miniprogram/pages/community/community.wxml'),'utf8').includes('item.thumbnailUrl || item.resultUrl'));
  p.onCommunityImageError({currentTarget:{dataset:{id:'post',kind:'result'}}});await tick();assert.equal(p.data.items[0].thumbnailUrl,hd);
 });
 await test('template_grid_small_first_cover_does_not_replace_detail_cover_array',()=>{
  const t=apiModule(),hd=signed('cover'),second=signed('second'),small=signed('cover','one',true),p=page('templates',t.api,t.a);
  p._renderData([],[{id:'tpl',name:'fixture',covers:[hd,second],cover:hd,thumbnail:small,cover_version:1}]);
  const item=p.data.allTemplates[0];assert.equal(item.coverUrl,small);assert.equal(item.cover,hd);assert.equal(item.covers[0],hd);assert.equal(item.covers[1],second);
 });
 fs.mkdirSync(out,{recursive:true});fs.writeFileSync(path.join(out,'load_media_frontend_results.json'),JSON.stringify({cases},null,2));
 const failed=cases.filter(x=>!x.passed).length;console.log(`LOAD_MEDIA_FRONTEND_SUMMARY total=${cases.length} passed=${cases.length-failed} failed=${failed}`);process.exitCode=failed?1:0;
})();
