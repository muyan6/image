/* Offline WeChat Page VM: catalog heat, dynamic community taxonomy and lifecycle. */
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const root=path.resolve(process.env.REVIEW_ROOT||path.join(__dirname,'../..'));
const out=path.resolve(process.env.REVIEW_OUTPUT||path.join(root,'audit/mini-discovery'));
const cases=[],tick=async()=>{for(let i=0;i<8;i++)await new Promise(r=>setImmediate(r));};
async function test(name,fn){try{await fn();cases.push({case:name,passed:true});console.log('PASS '+name);}catch(e){cases.push({case:name,passed:false,error:e.stack});console.error('FAIL '+name+': '+e.message);}}
function fixture(name,mocks={},stored={}){
 let page;const calls=[],remembered=[];const app={globalData:{lightPoints:200,freeMode:false,historyList:[]},setBalance(n){this.globalData.lightPoints=n;},persist(){}};
 const wx={getStorageSync:k=>stored[k],setStorageSync:(k,v)=>stored[k]=v,removeStorageSync:k=>delete stored[k],showToast(){},navigateTo(){},switchTab(){},stopPullDownRefresh(){calls.push('stop-refresh');}};
 const api={absolute:u=>u||'',stableImageUrl:(u,old)=>old&&old.replace(/q-signature=[^&]+/,'')===String(u).replace(/q-signature=[^&]+/,'')?old:u,
  announcements:async()=>({items:[]}),config:async()=>({prices:{light:40,fine:40},free_mode:false}),me:async()=>({balance:200}),
  templates:async()=>({groups:[],items:[]}),request:async()=>({enabled:true,categories:[],items:[],total:0,has_more:false,next_offset:0}),
  rememberCommunityImage:async u=>{remembered.push(u);},...mocks};
 vm.runInNewContext(fs.readFileSync(path.join(root,`miniprogram/pages/${name}/${name}.js`),'utf8'),{
  Page:p=>page=p,getApp:()=>app,require:n=>n.includes('creation-draft')?{readDraft:()=>null}:api,wx,
  console:{warn(){},log(){},error(){}},Date,setTimeout:()=>0,clearTimeout(){}});
 page.data=JSON.parse(JSON.stringify(page.data));page.setData=patch=>Object.assign(page.data,patch);
 return {page,api,app,wx,calls,remembered,stored};
}
const groups=[{id:'catalog_a',name:'真实分类甲',enabled:true},{id:'catalog_b',name:'真实分类乙',enabled:true}];
const template=(id,heat=0,extra={})=>({id,name:id,group_id:'catalog_a',usage_count:heat,price:40,
  cover:'https://cos.invalid/'+id+'.jpg',thumbnail:'https://cos.invalid/'+id+'_small.jpg',cover_version:1,...extra});
const post=(id,category='catalog_a')=>({id,category,group_id:category,categoryName:'真实分类',resultUrl:'https://cos.invalid/'+id+'.jpg',thumbnailUrl:'https://cos.invalid/'+id+'_small.jpg',origUrl:'',liked:false});
(async()=>{
 await test('home_has_no_hardcoded_presets_and_ranks_enabled_official_and_user_top_six',()=>{
  const f=fixture('index'),p=f.page;assert.equal(p.data.featuredTemplates.length,0);p._templateGroups=groups;
  p._renderFeatured([template('zero_z'),template('zero_a'),template('b',5),template('a',5),template('user_top',20,{source:'user'}),
    template('official_top',15,{source:'official'}),template('third',7),template('disabled',999,{enabled:false}),template('hidden_group',999,{group_id:'not_enabled'})]);
  assert.deepEqual(Array.from(p.data.featuredTemplates,x=>x.id),['user_top','official_top','third','a','b','zero_a']);
  assert(p.data.featuredTemplates.every(x=>x.costText==='✦ 40 光子'));
 });
 await test('successful_empty_or_disabled_catalog_clears_previous_home_ghosts',async()=>{
  let data={groups,items:[template('was_visible',8)]};const f=fixture('index',{templates:async()=>data});f.page._visible=true;
  await f.page.loadTemplates();assert.equal(f.page.data.featuredTemplates.length,1);
  data={groups,items:[]};await f.page.loadTemplates(true);assert.equal(f.page.data.featuredTemplates.length,0);
  data={groups:[],items:[template('disabled_group',50)]};await f.page.loadTemplates(true);assert.equal(f.page.data.featuredTemplates.length,0);
 });
 await test('old_unversioned_cache_is_not_rendered_on_failure_and_no_presets_return',async()=>{
  const f=fixture('index',{templates:async()=>{throw new Error('fixture offline');}},{cached_templates_items:[template('stale',100)]});
  f.page._visible=true;await f.page.loadTemplates();assert.equal(f.page.data.featuredTemplates.length,0);assert(f.page.data.templateError);
 });
 await test('home_initial_lifecycle_fetches_catalog_once_throttles_show_and_force_replaces_hot_rank',async()=>{
  let count=0,data={groups,items:[template('first',3),template('new_hot',0)]};
  const f=fixture('index',{templates:async()=>{count++;return data;}});f.page.onLoad({});f.page.onShow();await tick();
  assert.equal(count,1);f.page.onShow();await tick();assert.equal(count,1);
  data={groups,items:[template('first',3),template('new_hot',40)]};await f.page.onPullDownRefresh();
  assert.equal(count,2);assert.equal(f.page.data.featuredTemplates[0].id,'new_hot');assert.equal(f.calls.filter(x=>x==='stop-refresh').length,1);
 });
 await test('force_and_repeated_catalog_load_share_inflight_request_without_duplicate_downloads',async()=>{
  let resolve,count=0;const response=new Promise(r=>resolve=r);const f=fixture('index',{templates:()=>{count++;return response;}});
  const a=f.page.loadTemplates(),b=f.page.loadTemplates(true);assert.equal(a,b);assert.equal(count,1);
  resolve({groups,items:[template('preview')]});await a;
  f.page.onFeaturedImageLoad({currentTarget:{dataset:{id:'preview'}}});await tick();
  assert.deepEqual(f.remembered,['https://cos.invalid/preview_small.jpg']);
 });
 await test('community_categories_are_live_catalog_labels_and_server_pages_are_not_locally_cut',async()=>{
  const urls=[];const f=fixture('community',{request:async url=>{urls.push(url);return{enabled:true,categories:groups,items:[post('old_payload','film')],total:106,has_more:true,next_offset:24};}});
  f.page._visible=true;await f.page.loadCommunity(true);
  assert.deepEqual(Array.from(f.page.data.filters,x=>x.id),['all','liked','catalog_a','catalog_b']);
  await f.page.onSelectFilter({currentTarget:{dataset:{id:'catalog_a'}}});
  assert(urls.at(-1).includes('category=catalog_a'));assert.equal(f.page.data.filteredItems.length,1);
  assert.equal(f.page.data.total,106);assert.equal(f.page.data.hasMore,true);
 });
 await test('removed_active_catalog_group_400_recovers_all_once_without_polling',async()=>{
  const urls=[];const f=fixture('community',{request:async url=>{urls.push(url);if(url.includes('category=catalog_a'))throw Object.assign(new Error('deleted group'),{status:400});return{enabled:true,categories:[groups[1]],items:[post('remaining','catalog_b')],total:1,has_more:false,next_offset:1};}});
  f.page._visible=true;f.page.data.filters=[{id:'all',name:'全部展品'},{id:'liked',name:'我喜欢的'},...groups];f.page.data.activeFilter='catalog_a';
  await f.page.loadCommunity(true);assert.equal(urls.length,2);assert(!urls[1].includes('category='));
  assert.equal(f.page.data.activeFilter,'all');assert.equal(f.page.data.items[0].id,'remaining');assert(!f.page.data.filters.some(x=>x.id==='catalog_a'));
 });
 await test('renamed_categories_replace_tabs_and_legacy_anime_removal_reloads_authoritative_all',async()=>{
  let count=0;const f=fixture('community',{request:async()=>{count++;return count===1?{enabled:true,categories:[{id:'catalog_b',name:'改名乙'}],items:[],total:0,has_more:false,next_offset:0}:{enabled:true,categories:[{id:'catalog_b',name:'改名乙'}],items:[post('visible','catalog_b')],total:1,has_more:false,next_offset:1};}});
  f.page._visible=true;f.page.data.activeFilter='anime';f.page.data.filters=[{id:'all',name:'全部展品'},{id:'liked',name:'我喜欢的'},{id:'anime',name:'旧分类'}];
  await f.page.loadCommunity(true);assert.equal(count,2);assert.equal(f.page.data.activeFilter,'all');assert.equal(f.page.data.filters[2].name,'改名乙');
 });
 await test('liked_view_still_removes_just_unliked_items_and_group_like_name_does_not_collide',async()=>{
  const f=fixture('community',{request:async()=>({enabled:true,categories:[{id:'liked',name:'实际同名分组'}],items:[],total:0,has_more:false,next_offset:0})});
  f.page._visible=true;await f.page.loadCommunity(true);assert.equal(f.page.data.filters[2].id,'category:liked');
  f.page.data.items=[{id:'a',liked:true},{id:'b',liked:false}];f.page.filterItems('liked');assert.equal(f.page.data.filteredItems.length,1);
 });
 await test('existing_horizontal_showcase_and_centered_photo_entry_geometry_are_preserved',()=>{
  const html=fs.readFileSync(path.join(root,'miniprogram/pages/index/index.wxml'),'utf8'),css=fs.readFileSync(path.join(root,'miniprogram/pages/index/index.wxss'),'utf8');
  assert(html.includes('热门风格'));assert(html.includes('<scroll-view class="showcase-scroll" scroll-x'));assert(!html.includes('showcase-grid'));
  assert(/\.showcase-card\s*\{[^}]*width:\s*280rpx[^}]*height:\s*540rpx/.test(css));assert(/\.showcase-cover-wrap\s*\{[^}]*height:\s*320rpx/.test(css));
  assert(/\.portal-btn\s*\{[^}]*height:\s*96rpx[^}]*align-items:\s*center[^}]*justify-content:\s*center/.test(css));
  assert.equal(JSON.parse(fs.readFileSync(path.join(root,'miniprogram/pages/index/index.json'),'utf8')).enablePullDownRefresh,true);
 });
 fs.mkdirSync(out,{recursive:true});const passed=cases.filter(c=>c.passed).length;
 fs.writeFileSync(path.join(out,'mini_discovery_results.json'),JSON.stringify({cases},null,2));
 console.log(`MINI_DISCOVERY_SUMMARY total=${cases.length} passed=${passed} failed=${cases.length-passed}`);process.exitCode=passed===cases.length?0:1;
})().catch(e=>{console.error(e);process.exitCode=1;});
