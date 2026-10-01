/* Source-level page lifecycles plus actual API helpers; no network or account data. */
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const root=path.resolve(process.env.REVIEW_ROOT||path.resolve(__dirname,'../..'));
const out=path.resolve(process.env.REVIEW_OUTPUT||path.join(root,'audit/display-stability-tests')),rows=[];
let now=Date.now();class Clock extends Date{static now(){return now;}}
const tick=()=>new Promise(r=>setImmediate(r));
function app(){return {globalData:{apiBase:'https://api.invalid',mediaCache:{},historyList:[],lightPoints:100,freeMode:false},persist(){},setBalance(n){this.globalData.lightPoints=n;}};}
function apiModule(a=app()){
 let downloads=0,valid=new Set();const module={exports:{}};
 const wx={getFileSystemManager:()=>({accessSync(p){if(!valid.has(p))throw Error('missing temp image');}}),getImageInfo(o){downloads++;let p='wxfile://tmp/cache_'+downloads+'.jpg';valid.add(p);o.success({path:p});}};
 vm.runInNewContext(fs.readFileSync(path.join(root,'miniprogram/utils/api.js'),'utf8'),{module,wx,getApp:()=>a,Date:Clock,setTimeout,console});
 return {api:module.exports,a,valid,downloads:()=>downloads};
}
function page(name,api,a=app()){
 let p;vm.runInNewContext(fs.readFileSync(path.join(root,`miniprogram/pages/${name}/${name}.js`),'utf8'),{
  Page:x=>p=x,getApp:()=>a,require:()=>api,Date:Clock,console,setTimeout,clearTimeout,
  wx:{getStorageSync(){},setStorageSync(){},getWindowInfo:()=>({windowWidth:393}),stopPullDownRefresh(){},setNavigationBarTitle(){}}});
 p.data=JSON.parse(JSON.stringify(p.data));p.patches=[];p.setData=d=>{p.patches.push(d);Object.assign(p.data,d);};return p;
}
const signed=(id='cover',signature='one',ttl=300)=>`https://cos.invalid/${id}.jpg?q-sign-time=1%3B${Math.floor(now/1000)+ttl}&q-signature=${signature}`;
async function test(name,fn){try{let observed=await fn();rows.push({case:name,passed:true,observed});}catch(e){console.error(name,e);rows.push({case:name,passed:false,error:e.stack});}}
(async()=>{
 await test('stable_render_skips_identical_arrays_and_updates_real_changes',()=>{
  const {api}=apiModule(),p={data:{items:[{id:'one',url:'same'}]},writes:[],setData(x){this.writes.push(x);Object.assign(this.data,x);}};
  api.setDataStable(p,{items:[{id:'one',url:'same'}]});assert.equal(p.writes.length,0);
  api.setDataStable(p,{items:[{id:'one',url:'changed'}]});assert.equal(p.writes.length,1);
 });
 await test('cos_signature_rotation_retains_displayed_source_not_auth_query',()=>{
  const {api}=apiModule(),old=signed(),fresh=signed('cover','new');assert.equal(api.stableImageUrl(fresh,old,1,1),old);
  assert.equal(api.stableImageUrl(signed('changed','new'),old,1,1),signed('changed','new'));
 });
 await test('cache_identity_retains_transform_queries_and_cover_versions',async()=>{
  const t=apiModule(),base=signed();await t.api.rememberCommunityImage(base+'&imageMogr2/thumbnail/400x',1);
  await t.api.rememberCommunityImage(base.replace('one','two')+'&imageMogr2/thumbnail/400x',1);assert.equal(t.downloads(),1);
  await t.api.rememberCommunityImage(base+'&imageMogr2/thumbnail/800x',1);await t.api.rememberCommunityImage(base+'&imageMogr2/thumbnail/400x',2);assert.equal(t.downloads(),3);
 });
 await test('expired_signature_uses_local_file_missing_file_recovers_with_fresh_url',async()=>{
  const t=apiModule(),old=signed();await t.api.rememberCommunityImage(old);const local=t.api.communityImage(old);
  now+=360000;const fresh=signed('cover','after-expiry');assert.equal(t.api.stableImageUrl(fresh,old),local);
  t.valid.delete(local);assert.equal(t.api.stableImageUrl(fresh,old),fresh);now-=360000;
 });
 await test('template_metadata_refresh_does_not_rewrite_cover_arrays',()=>{
  const t=apiModule(),p=page('templates',t.api,t.a),item={id:'t_one',name:'胶片',group_id:'film',cover:signed(),covers:[signed()],cover_version:2};
  p._renderData([], [item]);const displayed=p.data.filteredTemplates[0].coverUrl;p.patches=[];
  for(let n=0;n<5;n++)p._renderData([], [{...item,cover:signed('cover','refresh'+n),covers:[signed('cover','refresh'+n)]}]);
  assert.equal(p.data.filteredTemplates[0].coverUrl,displayed);assert.equal(p.patches.length,0);
  p._renderData([], [{...item,name:'新名称',cover_version:3,cover:signed('cover_v3','three'),covers:[signed('cover_v3','three')]}]);assert.equal(p.data.filteredTemplates[0].name,'新名称');assert.notEqual(p.data.filteredTemplates[0].coverUrl,displayed);
  return {refreshes:5,imageArrayWrites:0};
 });
 await test('homepage_price_resync_does_not_remount_featured_images',()=>{
  const t=apiModule(),p=page('index',t.api,t.a),item={id:'t_one',cover:signed(),covers:[signed()],cover_version:1};
  p._renderFeatured([item]);p.patches=[];p._renderFeatured([{...item,cover:signed('cover','refresh'),covers:[signed('cover','refresh')]}]);assert.equal(p.patches.length,0);
  p.data.priceFine=80;p._renderFeatured([item]);assert(p.data.featuredTemplates[0].costText.includes('80'));
 });
 await test('community_refresh_preserves_images_and_real_like_changes',async()=>{
  const t=apiModule();let signature=0,likes=0;const mock={...t.api,request:async()=>({enabled:true,items:[{id:'s_one',title:'展品',resultUrl:signed('post','sig'+signature++),origUrl:'',likes}]})};
  const p=page('community',mock,t.a);await p.loadCommunity(true);const src=p.data.items[0].resultUrl;p.patches=[];
  await p.loadCommunity(true);assert.equal(p.data.items[0].resultUrl,src);assert(!p.patches.some(x=>x.items||x.filteredItems));
  likes=4;await p.loadCommunity(true);assert.equal(p.data.items[0].likes,4);assert.equal(p.data.items[0].resultUrl,src);
 });
 await test('profile_refresh_updates_balance_not_unchanged_thumbnail_projection',async()=>{
  const t=apiModule();let signature=0;const mock={...t.api,config:async()=>({}),me:async()=>({balance:125}),myJobs:async()=>({jobs:[{id:'job_one',status:'succeeded',quality:'fine',result_url:signed('result','sig'+signature++),orig_url:signed('original','sig'+signature)}]})};
  const p=page('my',mock,t.a);p.refreshUserData(true);await tick();p.patches=[];
  p.refreshUserData(true);await tick();assert(!p.patches.some(x=>x.previewWorks));assert.equal(p.data.lightPoints,125);
 });
 await test('works_metadata_refresh_retains_sources_and_clears_missing_cloud_image',async()=>{
  const t=apiModule();let signature=0,available=true;const mock={...t.api,myJobs:async()=>({jobs:[{id:'job_one',status:'succeeded',quality:'fine',result_url:available?signed('result','sig'+signature++):'',orig_url:signed('original','sig'+signature)}]})};
  const p=page('works',mock,t.a);await p.loadWorks(true);p.patches=[];await p.loadWorks(true);
  assert(!p.patches.some(x=>x.works||x.filteredWorks));available=false;await p.loadWorks(true);assert.equal(p.data.works[0].preview,'');
 });
 await test('compare_metadata_refresh_does_not_write_same_local_src',async()=>{
  const t=apiModule();t.valid.add('wxfile://tmp/result');let downloads=0;
  const mock={...t.api,request:async()=>({status:'succeeded',result_url:signed('result'),input_mode:'text'}),downloadJobMedia:async()=>{downloads++;return 'wxfile://tmp/result';}};
  const p=page('compare',mock,t.a);p._jobId='job_one';await p.refreshUrls(true);p.patches=[];await p.refreshUrls(true);
  assert.equal(downloads,1);assert(!p.patches.some(x=>x.resultUrl||x.originalUrl));t.valid.clear();await p.refreshUrls(true);assert.equal(downloads,2);
 });
 await test('template_search_icon_uses_fixed_geometry_not_font_baseline',()=>{
  const xml=fs.readFileSync(path.join(root,'miniprogram/pages/templates/templates.wxml'),'utf8'),css=fs.readFileSync(path.join(root,'miniprogram/pages/templates/templates.wxss'),'utf8');
  assert(!xml.includes('⌕'));assert(xml.includes('class="search-lens"'));assert(css.includes('flex:0 0 36rpx'));assert(css.includes('align-self:center'));
 });
 await test('admin_long_text_is_bounded_expandable_escaped_and_columns_fixed',()=>{
  const html=fs.readFileSync(path.join(root,'backend/admin.html'),'utf8');const start=html.indexOf('const esc ='),end=html.indexOf('const fmtTime',start);const c={};vm.createContext(c);vm.runInContext(html.slice(start,end),c);
  const value='<script>alert(1)</script>'+('LONG_WITHOUT_SPACES_'.repeat(100));const rendered=c.textCell(value);
  assert(rendered.includes('&lt;script&gt;'));assert(!rendered.includes('<script>'));assert(rendered.includes('<details'));assert(rendered.includes('展开完整内容'));assert(rendered.includes('LONG_WITHOUT_SPACES_'.repeat(100)));
  for(const token of ['table-layout:fixed','overflow-wrap:anywhere','-webkit-line-clamp:3','max-height:240px'])assert(html.includes(token));
  for(const name of ['jobs','grp','tpl','users','violations','submission','community','comment','report'])assert(html.includes('#'+name+'-table { min-width:'));
  assert((html.match(/tableColumns\(\[/g)||[]).length===9);return {tables:9,longTextCharacters:value.length,completeTextRetained:true};
 });
 await test('all_modified_image_load_handlers_are_present',()=>{
  for(const name of ['templates','my','works','index','style-detail','community']){
   const p=page(name,{}),xml=fs.readFileSync(path.join(root,`miniprogram/pages/${name}/${name}.wxml`),'utf8');
   for(const m of xml.matchAll(/bindload="(\w+)"/g))assert.equal(typeof p[m[1]],'function');
  }
 });
 fs.mkdirSync(out,{recursive:true});fs.writeFileSync(path.join(out,'display_stability_results.json'),JSON.stringify({cases:rows},null,2));
 console.log(`DISPLAY_STABILITY_SUMMARY total=${rows.length} passed=${rows.filter(x=>x.passed).length} failed=${rows.filter(x=>!x.passed).length}`);process.exitCode=rows.every(x=>x.passed)?0:1;
})();
