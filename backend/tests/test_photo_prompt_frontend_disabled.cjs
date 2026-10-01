const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const root=path.resolve(process.env.REVIEW_ROOT||path.resolve(__dirname,'../..'));
const out=path.resolve(process.env.REVIEW_OUTPUT||path.join(root,'audit/photo-prompt-off'));
const rows=[];
const app=()=>({globalData:{apiBase:'https://fixture.invalid',historyList:[],lightPoints:100},setBalance(){},persist(){}});
function page(api){let p;vm.runInNewContext(fs.readFileSync(path.join(root,'miniprogram/pages/adjust/adjust.js'),'utf8'),{
 Page:x=>p=x,require:()=>api,getApp:app,console,setTimeout,clearTimeout,
 wx:{showToast(){},showModal(){},redirectTo(){},hideLoading(){},vibrateShort(){}}});
 p.data=JSON.parse(JSON.stringify(p.data));p.setData=d=>Object.assign(p.data,d);return p;}
function apiFixture(cos=false){
 const calls=[],uploads=[],module={exports:{}};
 const wx={getStorageSync:()=> 'fixture-session',getFileSystemManager:()=>({statSync:()=>({size:9}),readFile:o=>o.success({data:new ArrayBuffer(9)})}),
  request:o=>{calls.push(o);let data={code:0,job_id:'abc123abc123',balance:100};
   if(o.url.endsWith('/api/config'))data={cos_ready:cos,cloud_pipeline:{enabled:cos},prices:{light:40,fine:80},template_quality_options:['light','fine']};
   if(o.url.endsWith('/api/uploads'))data={upload_id:'fixture',url:'https://cos.invalid/input'};
   o.success({statusCode:200,data});},
  uploadFile:o=>{uploads.push(o);o.success({statusCode:200,data:JSON.stringify({code:0,job_id:'abc123abc123',balance:100})});}};
 vm.runInNewContext(fs.readFileSync(path.join(root,'miniprogram/utils/api.js'),'utf8'),{module,getApp:app,wx,console,setTimeout,clearTimeout});
 return {api:module.exports,calls,uploads};
}
const hasPrompt=o=>Object.prototype.hasOwnProperty.call(o,'custom_prompt')||Object.prototype.hasOwnProperty.call(o,'customPrompt');
async function test(name,fn){try{await fn();rows.push({case:name,passed:true});}catch(e){rows.push({case:name,passed:false,error:e.message});console.error(name,e.message);}}
(async()=>{
 await test('repair_instruction_input_and_styles_are_absent',()=>{
  const xml=fs.readFileSync(path.join(root,'miniprogram/pages/adjust/adjust.wxml'),'utf8');
  const css=fs.readFileSync(path.join(root,'miniprogram/pages/adjust/adjust.wxss'),'utf8');
  assert(!xml.includes('补充要求'));assert(!xml.includes('onCustomPromptInput'));assert(!xml.includes('onToggleCustomPrompt'));
  assert(!xml.includes('<textarea'));assert(!css.includes('.custom-prompt-input'));assert(!css.includes('.prompt-toggle'));
 });
 await test('repair_page_has_no_instruction_draft_or_toggle_handlers',()=>{
  const p=page({});assert.equal(p.onCustomPromptInput,undefined);assert.equal(p.onToggleCustomPrompt,undefined);
  assert(!Object.prototype.hasOwnProperty.call(p.data,'customPrompt'));assert(!Object.prototype.hasOwnProperty.call(p.data,'showCustomPrompt'));
 });
 await test('stale_page_draft_is_not_sent_with_photo_submission',async()=>{
  let sent;const p=page({submitJob:async(file,data)=>{sent=data;return {code:0,job_id:'abc123abc123'};},absolute:x=>x,
   waitForJob:async()=>({status:'succeeded',result_url:'https://cos.invalid/result'})});
  p._foreground=true;Object.assign(p.data,{customPrompt:'stale directive',showCustomPrompt:true,quality:'light',currentRatioKey:'4:3'});
  await p.executeUpload('photo.jpg');assert(!hasPrompt(sent));assert.equal(sent.quality,'light');assert.equal(sent.aspect_ratio,'4:3');
 });
 await test('multipart_upload_drops_both_prompt_aliases_without_mutating_input',async()=>{
  const t=apiFixture(),input={quality:'light',custom_prompt:'stale',customPrompt:'stale alias',text_fields:'{"title":"caption"}'};
  await t.api.upload('photo.jpg',input);assert(!hasPrompt(t.uploads[0].formData));assert.equal(input.custom_prompt,'stale');
  assert.equal(t.uploads[0].formData.text_fields,input.text_fields);
 });
 await test('by_upload_and_generic_photo_json_calls_drop_instruction_fields',async()=>{
  const t=apiFixture(),input={upload_id:'fixture',quality:'fine',custom_prompt:'stale',customPrompt:'alias',expected_price:80};
  await t.api.rescueByUpload(input);await t.api.request('/api/rescue/by-upload?check=1',{method:'POST',data:input});
  assert(t.calls.every(o=>!hasPrompt(o.data)));assert.equal(t.calls[0].data.expected_price,80);assert.equal(input.customPrompt,'alias');
 });
 await test('serialized_photo_request_data_also_drops_instruction_fields',async()=>{
  const t=apiFixture();const input=JSON.stringify({upload_id:'fixture',quality:'light',custom_prompt:'stale',customPrompt:'alias'});
  await t.api.request('/api/rescue/by-upload',{method:'POST',data:input});
  const sent=JSON.parse(t.calls[0].data);assert(!hasPrompt(sent));assert.equal(sent.upload_id,'fixture');
 });
 await test('cos_submission_does_not_forward_legacy_instruction_or_lose_template_text',async()=>{
  const t=apiFixture(true);const input={quality:'light',custom_prompt:'stale',customPrompt:'alias',template_id:'poster',text_fields:{title:'caption'},expected_price:40};
  await t.api.submitJob('photo.jpg',input);const sent=t.calls.find(o=>o.url.endsWith('/api/rescue/by-upload')).data;
  assert(!hasPrompt(sent));assert.equal(sent.template_id,'poster');assert.equal(JSON.parse(sent.text_fields).title,'caption');assert.equal(sent.expected_price,'40');
 });
 await test('template_caption_input_and_output_modes_are_retained',()=>{
  const xml=fs.readFileSync(path.join(root,'miniprogram/pages/adjust/adjust.wxml'),'utf8'),p=page({});
  assert(xml.includes('onTextFieldInput'));assert(xml.includes('tpl-text-input'));assert(xml.includes('onSelectTemplateOutput'));
  assert.equal(typeof p.onTextFieldInput,'function');assert.equal(typeof p.onSelectTemplateOutput,'function');
 });
 fs.mkdirSync(out,{recursive:true});fs.writeFileSync(path.join(out,'photo_prompt_frontend_disabled_results.json'),JSON.stringify({cases:rows},null,2));
 const failed=rows.filter(x=>!x.passed).length;console.log(`PHOTO_PROMPT_FRONTEND_DISABLED_SUMMARY total=${rows.length} passed=${rows.length-failed} failed=${failed}`);process.exitCode=failed?1:0;
})();
