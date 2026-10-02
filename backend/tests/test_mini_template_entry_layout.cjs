/* Offline entry/layout contracts plus the real page handlers and error states. */
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const root=path.resolve(process.env.REVIEW_ROOT||path.resolve(__dirname,'../..'));
const out=path.resolve(process.env.REVIEW_OUTPUT||path.join(root,'audit/mini-template-entry-layout-tests')),cases=[];
const source=(page,ext)=>fs.readFileSync(path.join(root,`miniprogram/pages/${page}/${page}.${ext}`),'utf8');
function page(name,preferences={}){
 let p;const navigations=[],app={globalData:{apiBase:'https://fixture.invalid',historyList:[],mediaCache:{}},persist(){}};
 const api={absolute:x=>x||'',config:async()=>({}),templates:async()=>({items:[],groups:[]})};
 const wx={navigateTo:x=>navigations.push(x.url),getStorageSync(){},setStorageSync(){},getWindowInfo:()=>({windowWidth:375})};
 vm.runInNewContext(source(name,'js'),{Page:x=>p=x,getApp:()=>app,require:n=>n.includes('preferences.js')?preferences:api,wx,console:{warn(){}},Date,Math,setTimeout,clearTimeout});
 p.data=JSON.parse(JSON.stringify(p.data));p.setData=patch=>Object.assign(p.data,patch);return {p,navigations};
}
function style(css,selector){
 const out={};css=css.replace(/\/\*[\s\S]*?\*\//g,'');
 for(const rule of css.matchAll(/([^{}]+)\{([^{}]*)\}/g))if(rule[1].split(',').some(x=>x.trim()===selector)){
  for(const d of rule[2].matchAll(/([a-z-]+)\s*:\s*([^;]+)(?:;|$)/g))out[d[1]]=d[2].trim();
 }
 return out;
}
const rpx=value=>Number(String(value).replace(/rpx$/,''));
async function test(name,fn){try{await fn();cases.push({case:name,passed:true});console.log('PASS '+name);}catch(error){cases.push({case:name,passed:false,error:error.stack});console.error('FAIL '+name,error);}}
(async()=>{
 await test('catalog_header_has_no_contribution_actions_or_orphan_handlers',()=>{
  for(const ext of ['js','wxml','wxss'])assert(!/catalog-contribute|onSubmitTemplate|onMyTemplates/.test(source('templates',ext)));
  const xml=source('templates','wxml');assert(xml.includes('template-search'));assert(xml.includes('category-bar'));
 });
 await test('profile_menu_contains_two_distinct_actions_and_regular_dividers',()=>{
  const xml=source('my','wxml');assert(!xml.includes('我的分享模板'));
  assert(/class="menu-item" bindtap="onShareTemplate"><text class="menu-label">分享我的模板<\/text>/.test(xml));
  assert(/class="menu-item" bindtap="onGoTemplates"><text class="menu-label">我的模板<\/text>/.test(xml));
  assert(/bindtap="onShareTemplate">[\s\S]*?<\/view>\s*<view class="menu-divider"><\/view>\s*<view class="menu-item" bindtap="onGoTemplates">/.test(xml));
  assert(/bindtap="onGoTemplates">[\s\S]*?<\/view>\s*<view class="menu-divider"><\/view>\s*<view class="menu-item" bindtap="onGoCredits">/.test(xml));
 });
 await test('new_actions_use_real_create_and_owner_listing_routes',()=>{
  const {p,navigations}=page('my');p.onShareTemplate();p.onGoTemplates();
  assert.equal(navigations.join('|'),'/pages/template-share/template-share|/pages/template-shares/template-shares');
 });
 await test('share_entries_are_not_inserted_into_balance_or_stat_areas',()=>{
  const xml=source('my','wxml'),beforeMenu=xml.slice(0,xml.indexOf('class="menu-card gallery-card"'));
  assert(!beforeMenu.includes('onShareTemplate'));assert(!beforeMenu.includes('onGoTemplates'));assert(!beforeMenu.includes('分享我的模板'));
 });
 await test('menu_rows_keep_centered_regular_touch_area_without_contact_compression',()=>{
  const css=source('my','wxss'),row=style(css,'.menu-item'),contact=style(css,'.menu-contact-action'),label=style(css,'.menu-label');
  assert.equal(row.display,'flex');assert.equal(row['align-items'],'center');assert(rpx(row['min-height'])>=80);
  assert.equal(contact.flex,'1');assert.equal(contact['min-width'],'0');assert.equal(contact.height,'auto');assert.equal(contact['text-align'],'left');
  assert.equal(contact['font-size'],label['font-size']);assert.equal(contact['line-height'],label['line-height']);
  assert.equal(style(css,'.menu-arrow')['flex-shrink'],'0');
 });
 await test('search_preference_category_and_sort_controls_have_consistent_touch_capsules',()=>{
  const css=source('templates','wxss');
  for(const selector of ['.preference-chip','.cat-chip','.sort-chip']){
   const s=style(css,selector);assert.equal(s['box-sizing'],'border-box');assert.equal(s.display,'inline-flex');assert.equal(s['align-items'],'center');
   assert.equal(s['font-size'],'24rpx');assert.equal(s['border-radius'],'36rpx');assert.equal(s['min-height'],'72rpx');
  }
  assert.equal(style(css,'.template-search')['box-sizing'],'border-box');assert(rpx(style(css,'.template-search')['min-height'])>=80);
  assert(rpx(style(css,'.search-clear')['min-height'])>=68);assert(rpx(style(css,'.sort-shuffle')['min-height'])>=68);
 });
 await test('filter_rows_use_regular_positive_spacing_not_overlapping_negative_margins',()=>{
  const css=source('templates','wxss');assert.equal(style(css,'.template-search').margin,'0 0 16rpx');
  assert.equal(style(css,'.preference-bar').margin,'0 0 16rpx');assert.equal(style(css,'.category-bar')['margin-bottom'],'16rpx');
  assert.equal(style(css,'.catalog-sort').margin,'0 0 24rpx');assert.equal(style(css,'.preference-bar').gap,'12rpx');
  assert.equal(style(css,'.catalog-sort').gap,'12rpx');
 });
 await test('preference_404_is_explained_without_faking_success_or_erasing_saved_ids',async()=>{
  const {p}=page('templates',{load:async()=>{throw Object.assign(new Error('Not Found'),{status:404});}});
  p.data.favoriteIds=['kept'];await p.loadPreferences();
  assert.equal(p.data.preferencesError,'收藏与最近使用接口未找到（404），请核对后端更新');assert.equal(p.data.favoriteIds[0],'kept');assert.equal(p.data.preferencesLoading,false);
 });
 await test('non_404_preference_error_keeps_original_message',async()=>{
  const {p}=page('templates',{load:async()=>{throw Object.assign(new Error('网络连接中断'),{status:503});}});
  await p.loadPreferences();assert.equal(p.data.preferencesError,'网络连接中断');
 });
 await test('catalog_inline_errors_remain_visible_and_retry_is_compact',()=>{
  const xml=source('templates','wxml'),css=source('templates','wxss');
  assert(xml.includes('wx:if="{{preferencesError}}"'));assert(xml.includes('{{preferencesError}}'));assert(xml.includes('bindtap="loadPreferences"'));
  assert(xml.includes('bindtap="onRetryTemplates"'));assert(xml.includes('catalog-retry'));
  const button=style(css,'.catalog-retry');assert.equal(button.width,'auto');assert.equal(button.flex,'0 0 auto');assert(rpx(button['min-height'])>=68);
  assert.equal(style(css,'.catalog-state-message').flex,'1');assert.equal(style(css,'.catalog-state')['flex-wrap'],'wrap');
 });
 await test('owner_listing_error_text_and_request_flow_stay_real_with_compact_retry',()=>{
  const xml=source('template-shares','wxml'),css=source('template-shares','wxss');
  assert(xml.includes('{{error}}'));assert(xml.includes('wx:if="{{error}}"'));assert(xml.includes('class="notice-retry" bindtap="onRetry"'));
  const button=style(css,'.notice-retry');assert.equal(button.width,'auto');assert.equal(button.flex,'0 0 auto');assert(rpx(button['min-height'])>=68);
  assert(source('template-shares','js').includes("error:e.message||'我的模板读取失败'"));
 });
 await test('preference_endpoint_name_matches_backend_registration_without_api_change',()=>{
  const helper=fs.readFileSync(path.join(root,'miniprogram/utils/preferences.js'),'utf8');
  const backend=fs.readFileSync(path.join(root,'backend/experience_api.py'),'utf8');
  assert(helper.includes("api.request('/api/me/preferences')"));assert(backend.includes("@router.get('/api/me/preferences')"));
 });
 await test('hot_latest_random_and_regular_category_sort_still_work',()=>{
  const {p}=page('templates');p._renderData([{id:'g',name:'分类'}],[{id:'official',group_id:'g',sort:1,usage_count:2,published_at:1},{id:'user',group_id:'',source:'user',usage_count:10,published_at:3},{id:'high',group_id:'g',sort:2,usage_count:9,published_at:2}]);
  assert.equal(p.data.filteredTemplates.map(x=>x.id).join(','),'user,high,official');
  p.onSortMode({currentTarget:{dataset:{id:'latest'}}});assert.equal(p.data.filteredTemplates[0].id,'user');
  p.onSortMode({currentTarget:{dataset:{id:'random'}}});const order=p.data.filteredTemplates.map(x=>x.id).join(',');p.filterByCategory('all');assert.equal(p.data.filteredTemplates.map(x=>x.id).join(','),order);
  p.filterByCategory('g');assert.equal(p.data.filteredTemplates.map(x=>x.id).join(','),'official,high');
 });
 await test('grid_uses_small_cover_and_preserves_detail_covers',()=>{
  const {p}=page('templates');p._renderData([],[{id:'cover',thumbnail:'https://cos.invalid/small',covers:['https://cos.invalid/hd','https://cos.invalid/second']}]);
  const t=p.data.allTemplates[0];assert.equal(t.coverUrl,'https://cos.invalid/small');assert.equal(t.covers[0],'https://cos.invalid/hd');assert.equal(t.covers.length,2);
 });
 fs.mkdirSync(out,{recursive:true});fs.writeFileSync(path.join(out,'mini_template_entry_layout_results.json'),JSON.stringify({cases},null,2),'utf8');
 const passed=cases.filter(x=>x.passed).length;console.log(`MINI_TEMPLATE_ENTRY_LAYOUT_SUMMARY total=${cases.length} passed=${passed} failed=${cases.length-passed}`);process.exitCode=passed===cases.length?0:1;
})().catch(error=>{console.error(error);process.exitCode=1;});
