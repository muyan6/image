/* Real mini catalog state transitions; mock navigation/API only, no live account data. */
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const root=path.resolve(process.env.REVIEW_ROOT||path.resolve(__dirname,'../..'));
const out=path.resolve(process.env.REVIEW_OUTPUT||path.join(root,'audit/mini-catalog-simplify-tests')),cases=[];
const read=(name,ext)=>fs.readFileSync(path.join(root,`miniprogram/pages/${name}/${name}.${ext}`),'utf8');
function page(name='templates',overrides={}){
 let p;const calls=[],app={globalData:{historyList:[],mediaCache:{},apiBase:'https://fixture.invalid'},persist(){}};
 const api={absolute:x=>x||'',...overrides};
 vm.runInNewContext(read(name,'js'),{Page:x=>p=x,getApp:()=>app,require:()=>api,console,setTimeout,clearTimeout,Date,Math,
  wx:{navigateTo:x=>calls.push(x.url),getStorageSync(){},setStorageSync(){},getWindowInfo:()=>({windowWidth:375})}});
 p.data=JSON.parse(JSON.stringify(p.data));p.setData=x=>Object.assign(p.data,x);return {p,calls};
}
function fixture(){const {p}=page();p._renderData([{id:'g1',name:'一类'},{id:'g2',name:'二类'}],[
 {id:'one',name:'第一个完整名称',subtitle:'独特胶片简介',group_id:'g1',sort:2,usage_count:1,published_at:1},
 {id:'two',name:'第二个完整名称',subtitle:'自然水彩',group_id:'g1',sort:1,usage_count:8,published_at:2},
 {id:'three',name:'第三个完整名称',subtitle:'远处风景',group_id:'g2',sort:1,usage_count:3,published_at:3},
 {id:'shared',name:'用户分享',subtitle:'柔和颜色',group_id:'',source:'user',usage_count:12,published_at:4}]);return p;}
const ids=p=>p.data.filteredTemplates.map(x=>x.id).join(',');
function style(css,selector){const out={};css=css.replace(/\/\*[\s\S]*?\*\//g,'');for(const r of css.matchAll(/([^{}]+)\{([^{}]*)\}/g))if(r[1].split(',').some(x=>x.trim()===selector))for(const d of r[2].matchAll(/([a-z-]+)\s*:\s*([^;]+)(?:;|$)/g))out[d[1]]=d[2].trim();return out;}
async function test(name,fn){try{await fn();cases.push({case:name,passed:true});console.log('PASS '+name);}catch(error){cases.push({case:name,passed:false,error:error.stack});console.error('FAIL '+name,error);}}
(async()=>{
 await test('profile_has_only_one_my_templates_entry_not_a_create_shortcut',()=>{
  const xml=read('my','wxml');assert.equal((xml.match(/bindtap="onGoTemplates"/g)||[]).length,1);assert(!xml.includes('onShareTemplate'));
  const {p,calls}=page('my');assert.equal(p.onShareTemplate,undefined);p.onGoTemplates();assert.equal(calls[0],'/pages/template-shares/template-shares');
  assert(read('template-shares','wxml').includes('bindtap="onCreate"'));
 });
 await test('scope_tabs_stay_one_row_and_two_pickers_share_one_all_only_filter_row',()=>{
  const xml=read('templates','wxml');assert(!xml.includes('category-bar'));assert(!xml.includes('catalog-sort'));
  assert(xml.includes('class="catalog-filters" wx:if="{{preferenceMode === \'all\'}}"'));
  const row=xml.slice(xml.indexOf('class="catalog-filters"'),xml.indexOf('<!-- 模板展柜'));
  assert.equal((row.match(/<picker /g)||[]).length,2);assert(row.includes('onCategoryChange'));assert(row.includes('onSortChange'));
  assert.equal(style(read('templates','wxss'),'.preference-bar')['flex-wrap'],'nowrap');
 });
 await test('category_picker_uses_only_all_and_current_server_groups',()=>{
  const p=fixture();assert.equal(p.data.categories.map(x=>x.id).join(','),'all,g1,g2');
  p.onCategoryChange({detail:{value:'1'}});assert.equal(p.data.activeCategory,'g1');assert.equal(p.data.categoryLabel,'一类');assert.equal(ids(p),'two,one');
  p.onCategoryChange({detail:{value:'99'}});assert.equal(p.data.activeCategory,'g1');
 });
 await test('ordinary_category_marks_backend_order_and_disables_inapplicable_sort',()=>{
  const p=fixture();p.onSortChange({detail:{value:'1'}});assert.equal(p.data.sortMode,'latest');
  p.onCategoryChange({detail:{value:'1'}});assert.equal(p.data.sortLabel,'分类顺序');assert.equal(ids(p),'two,one');
  p.onSortChange({detail:{value:'2'}});assert.equal(p.data.sortMode,'latest');assert.equal(ids(p),'two,one');
  assert(read('templates','wxml').includes('disabled="{{activeCategory !== \'all\'}}"'));
 });
 await test('favorites_ignore_previously_selected_category_and_hidden_global_sort',()=>{
  const p=fixture();p.data.favoriteIds=['three','one'];p.onSortChange({detail:{value:'2'}});p.onCategoryChange({detail:{value:'1'}});
  p.onPreferenceFilter({currentTarget:{dataset:{id:'favorites'}}});assert.equal(ids(p),'three,one');assert.equal(p.data.activeCategory,'g1');
  p.onCategoryChange({detail:{value:'2'}});assert.equal(p.data.activeCategory,'g1');assert.equal(ids(p),'three,one');
 });
 await test('recent_ignores_category_and_uses_user_recent_sequence_with_search',()=>{
  const p=fixture();p.data.recentIds=['three','one','shared'];p.onCategoryChange({detail:{value:'1'}});
  p.onPreferenceFilter({currentTarget:{dataset:{id:'recent'}}});assert.equal(ids(p),'three,one,shared');
  p.onSearchInput({detail:{value:'胶片'}});assert.equal(ids(p),'one');p.onClearSearch();assert.equal(ids(p),'three,one,shared');
 });
 await test('returning_to_all_restores_category_selection_and_backend_order',()=>{
  const p=fixture();p.data.favoriteIds=['three'];p.onCategoryChange({detail:{value:'1'}});p.onPreferenceFilter({currentTarget:{dataset:{id:'favorites'}}});
  assert.equal(ids(p),'three');p.onPreferenceFilter({currentTarget:{dataset:{id:'all'}}});assert.equal(ids(p),'two,one');assert.equal(p.data.categoryIndex,1);
 });
 await test('hot_latest_and_random_retained_without_changing_heat_counts',()=>{
  const p=fixture();assert.equal(ids(p),'shared,two,three,one');p.onSortChange({detail:{value:'1'}});assert.equal(ids(p),'shared,three,two,one');
  p.onSortChange({detail:{value:'2'}});const ranks=p._randomRanks,order=ids(p);p.filterByCategory('all');assert.equal(ids(p),order);assert.strictEqual(p._randomRanks,ranks);
  p.onShuffleTemplates();assert.notStrictEqual(p._randomRanks,ranks);assert.equal(p.data.allTemplates.reduce((n,x)=>n+x.usage_count,0),24);
 });
 await test('subtitle_is_hidden_only_on_cards_still_searchable_and_preserved_for_details',()=>{
  const xml=read('templates','wxml');assert(!xml.includes('class="card-subtitle"'));assert(xml.includes('{{ selectedItem.subtitle }}'));
  const p=fixture();p.onSearchInput({detail:{value:'独特胶片简介'}});assert.equal(ids(p),'one');assert.equal(p.data.filteredTemplates[0].subtitle,'独特胶片简介');
 });
 await test('long_titles_keep_full_data_and_accessible_name_use_single_line_ellipsis',()=>{
  const {p}=page();const name='二十个字的完整用户模板长名称原文保存';p._renderData([],[{id:'long',name,prompt:'完整提示词'}]);
  assert.equal(p.data.allTemplates[0].name,name);assert.equal(p.data.allTemplates[0].prompt,'完整提示词');
  const s=style(read('templates','wxss'),'.card-name');assert.equal(s.flex,'1');assert.equal(s['min-width'],'0');assert.equal(s['white-space'],'nowrap');assert.equal(s['text-overflow'],'ellipsis');assert.equal(s.overflow,'hidden');assert(Number(s['font-size'].replace('rpx',''))>=32);
  assert(read('templates','wxml').includes('aria-label="{{item.name}}"'));
 });
 await test('favorite_star_is_separate_right_hand_touch_target',()=>{
  const s=style(read('templates','wxss'),'.favorite-btn');assert.equal(s.flex,'0 0 72rpx');assert.equal(s.width,'72rpx');assert.equal(s.height,'72rpx');assert.equal(s.margin,'0 0 0 auto');
  assert(read('templates','wxml').includes('catchtap="onFavorite"'));assert.equal(style(read('templates','wxss'),'.card-name-row')['align-items'],'center');
 });
 await test('controls_and_cards_are_square_while_magnifier_lens_remains_round',()=>{
  const css=read('templates','wxss');for(const selector of ['.template-search','.preference-chip','.catalog-picker-trigger','.sort-shuffle','.favorite-btn','.template-card','.catalog-retry'])assert.equal(style(css,selector)['border-radius'],'0');
  assert.equal(style(css,'.search-lens')['border-radius'],'50%');
 });
 await test('controls_minimum_touch_size_and_random_shuffle_stays_in_same_row',()=>{
  const css=read('templates','wxss');for(const selector of ['.preference-chip','.catalog-picker-trigger','.sort-shuffle'])assert(Number(style(css,selector)['min-height'].replace('rpx',''))>=72);
  const xml=read('templates','wxml');assert(xml.includes('wx:if="{{activeCategory === \'all\' && sortMode === \'random\'}}"'));assert(!style(css,'.catalog-filters')['flex-wrap']);
 });
 await test('small_media_previews_and_hd_detail_arrays_are_not_replaced',()=>{
  const {p}=page();p._renderData([],[{id:'media',name:'模板',thumbnail:'https://cos.invalid/small',covers:['https://cos.invalid/hd','https://cos.invalid/hd2']}]);
  const item=p.data.allTemplates[0];assert.equal(item.coverUrl,'https://cos.invalid/small');assert.equal(item.covers[0],'https://cos.invalid/hd');assert.equal(item.covers.length,2);
 });
 fs.mkdirSync(out,{recursive:true});fs.writeFileSync(path.join(out,'mini_catalog_simplify_results.json'),JSON.stringify({cases},null,2),'utf8');
 const passed=cases.filter(x=>x.passed).length;console.log(`MINI_CATALOG_SIMPLIFY_SUMMARY total=${cases.length} passed=${passed} failed=${cases.length-passed}`);process.exitCode=passed===cases.length?0:1;
})().catch(error=>{console.error(error);process.exitCode=1});
