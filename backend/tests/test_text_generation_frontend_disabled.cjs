/* Client feature removal, independently of server model enablement. */
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const root=path.resolve(process.env.REVIEW_ROOT||path.resolve(__dirname,'../..'));
const out=path.resolve(process.env.REVIEW_OUTPUT||path.join(root,'audit/text-entry-off'));
const rows=[];
function apiFixture(){
 let requests=0,logins=0;const module={exports:{}};
 const app={globalData:{apiBase:'https://fixture.invalid',lightPoints:100},setBalance(n){this.globalData.lightPoints=n;}};
 const wx={getStorageSync:()=> 'fixture-session',setStorageSync(){},login(){logins++;},
  request(o){requests++;o.success({statusCode:200,data:{job_id:'fixture',text_generation:{ready:true},balance:100}});}};
 vm.runInNewContext(fs.readFileSync(path.join(root,'miniprogram/utils/api.js'),'utf8'),{module,getApp:()=>app,wx,console,setTimeout,clearTimeout});
 return {api:module.exports,requests:()=>requests,logins:()=>logins};
}
async function test(name,fn){try{await fn();rows.push({case:name,passed:true});}catch(e){rows.push({case:name,passed:false,error:e.message});console.error(name,e.message);}}
(async()=>{
 await test('homepage_has_no_visible_text_generation_entry_or_navigation',()=>{
  const xml=fs.readFileSync(path.join(root,'miniprogram/pages/index/index.wxml'),'utf8');
  const js=fs.readFileSync(path.join(root,'miniprogram/pages/index/index.js'),'utf8');
  assert(!xml.includes('text-generation-entry'));assert(!xml.includes('文字生图'));
  assert(!js.includes('onOpenTextGeneration'));assert(!js.includes('/pages/text-generation/'));
 });
 await test('text_generation_page_is_not_registered_or_packaged',()=>{
  assert(!JSON.parse(fs.readFileSync(path.join(root,'miniprogram/app.json'))).pages.some(p=>p.includes('text-generation')));
  for(const ext of ['js','json','wxml','wxss'])
    assert(!fs.existsSync(path.join(root,'miniprogram/pages/text-generation/text-generation.'+ext)));
 });
 await test('all_registered_photo_and_template_pages_are_retained',()=>{
  const pages=JSON.parse(fs.readFileSync(path.join(root,'miniprogram/app.json'))).pages;
  for(const name of ['index','adjust','templates','style-detail','works','compare'])assert(pages.includes(`pages/${name}/${name}`));
  for(const p of pages)for(const ext of ['.js','.json','.wxml','.wxss'])assert(fs.existsSync(path.join(root,'miniprogram',p+ext)));
 });
 await test('official_client_rejects_text_submission_without_http_or_login',async()=>{
  const t=apiFixture();await assert.rejects(t.api.request('/api/text-generation',{method:'POST',data:{prompt:'fixture'}}),e=>e.code==='FEATURE_DISABLED');
  assert.equal(t.requests(),0);assert.equal(t.logins(),0);
 });
 await test('query_and_trailing_slash_text_paths_are_also_rejected',async()=>{
  const t=apiFixture();for(const p of ['/api/text-generation/','/api/text-generation?mode=1'])await assert.rejects(t.api.request(p,{method:'POST'}),e=>e.code==='FEATURE_DISABLED');
  assert.equal(t.requests(),0);
 });
 await test('server_text_ready_config_does_not_enable_removed_client_feature',async()=>{
  const t=apiFixture();const c=await t.api.config();assert(c.text_generation.ready);
  const before=t.requests();await assert.rejects(t.api.request('/api/text-generation',{method:'POST'}),e=>e.code==='FEATURE_DISABLED');assert.equal(t.requests(),before);
 });
 await test('photo_submission_api_and_public_config_remain_available',async()=>{
  const t=apiFixture();await t.api.rescueByUpload({upload_id:'fixture',quality:'light'});await t.api.config();assert.equal(t.requests(),2);
 });
 await test('backend_text_generation_route_and_admin_configuration_are_retained',()=>{
  const py=fs.readFileSync(path.join(root,'backend/text_generation.py'),'utf8'),main=fs.readFileSync(path.join(root,'backend/main.py'),'utf8');
  assert(py.includes("@router.post('/api/text-generation')"));assert(main.includes('app.include_router(make_text_router('));
  assert(fs.readFileSync(path.join(root,'backend/admin.html'),'utf8').includes('文生图 · 独立网关配置'));
 });
 fs.mkdirSync(out,{recursive:true});fs.writeFileSync(path.join(out,'text_generation_frontend_disabled_results.json'),JSON.stringify({cases:rows},null,2));
 const failed=rows.filter(x=>!x.passed).length;console.log(`TEXT_FRONTEND_DISABLED_SUMMARY total=${rows.length} passed=${rows.length-failed} failed=${failed}`);process.exitCode=failed?1:0;
})();
