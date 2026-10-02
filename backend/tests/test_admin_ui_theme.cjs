/* Offline paper-theme/CSS contracts. Optional baseline parity is for a CSS-only
 * change audit, not a permanent prohibition on later administrative features.
 * Set ADMIN_UI_BASELINE_REF=3297bef or ADMIN_UI_BASELINE_FILE for that audit.
 */
const fs=require('fs'),path=require('path'),assert=require('assert'),vm=require('vm');
const {execFileSync}=require('child_process'),crypto=require('crypto'),zlib=require('zlib');
const {JSDOM,VirtualConsole}=require(process.env.WEB_DOM_MODULE||'jsdom');
const root=path.resolve(process.env.REVIEW_ROOT||path.join(__dirname,'../..'));
const output=path.resolve(process.env.REVIEW_OUTPUT||path.join(root,'audit/admin-ui-theme'));
const bytes=fs.readFileSync(path.join(root,'backend/admin.html')),html=bytes.toString('utf8');
const styleMatches=[...html.matchAll(/<style\b[^>]*>([\s\S]*?)<\/style>/g)];
assert.equal(styleMatches.length,1,'exactly one existing stylesheet');
const css=styleMatches[0][1],scriptMatches=[...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)];
const script=scriptMatches.map(x=>x[1]).join('\n');
const sha=value=>crypto.createHash('sha256').update(value).digest('hex');
const canonical=value=>value.replace(/\r+\n/g,'\n');
const cases=[],observations={script_raw_sha256:sha(script),script_canonical_sha256:sha(canonical(script)),
  html_sha256:sha(bytes),css_bytes:Buffer.byteLength(css),css_gzip_bytes:zlib.gzipSync(css).length,
  external_resources_added:false,rendered_browser_geometry_measured:false};
const errors=[],virtualConsole=new VirtualConsole();virtualConsole.on('jsdomError',e=>errors.push(e.message));
const dom=new JSDOM(html,{url:'https://fixture.invalid/admin',runScripts:'outside-only',virtualConsole});
const sheet=dom.window.document.styleSheets[0];
function flatten(rules,media=''){
  const out=[];for(const rule of rules){if(rule.selectorText)out.push({rule,media});else if(rule.cssRules)out.push(...flatten(rule.cssRules,rule.conditionText||media));}return out;
}
const rules=flatten(sheet.cssRules);
function rule(selector,media=''){
  const matches=rules.filter(x=>x.media===media&&x.rule.selectorText.split(',').map(s=>s.trim()).includes(selector));
  assert(matches.length,'missing CSS rule '+selector+' @ '+media);
  return {getPropertyValue(key){return matches.map(x=>x.rule.style.getPropertyValue(key)).filter(Boolean).at(-1)||'';}};
}
const value=(selector,key,media='')=>rule(selector,media).getPropertyValue(key).trim().replace(/^0px$/,'0');
const vars=rule(':root');
function token(name){let result=vars.getPropertyValue(name).trim();let depth=0;while(/^var\(/.test(result)){assert(++depth<12);result=vars.getPropertyValue(result.match(/^var\((--[\w-]+)\)$/)[1]).trim();}return result;}
function luminance(hex){const rgb=hex.replace('#','').match(/../g).map(x=>parseInt(x,16)/255).map(x=>x<=.04045?x/12.92:((x+.055)/1.055)**2.4);return .2126*rgb[0]+.7152*rgb[1]+.0722*rgb[2];}
function contrast(fg,bg){const a=luminance(fg),b=luminance(bg);return (Math.max(a,b)+.05)/(Math.min(a,b)+.05);}
function test(name,body){try{body();cases.push({case:name,passed:true});console.log('PASS '+name);}catch(error){cases.push({case:name,passed:false,error:error.stack});console.error('FAIL '+name+': '+error.message);}}

test('paper_palette_uses_shared_native_frontend_surfaces',()=>{
  assert.equal(token('--paper-bg'),'#f5f2eb');assert.equal(token('--paper-card'),'#fffdf9');
  assert.equal(token('--ink'),'#342d24');assert.equal(token('--accent-gold'),'#a47b42');
  assert.equal(token('--radius'),'8px');assert.equal(value('body','background'),'var(--bg)');
  assert.equal(value('.card','background'),'var(--card)');
});
test('paper_sidebar_has_visible_warm_active_state_not_dark_blue_gradient',()=>{
  assert.equal(value('.sidebar','background'),'var(--sidebar)');assert.equal(token('--sidebar'),'#f0ebe1');
  assert.equal(value('.nav-item.on','background'),'var(--brand-weak)');
  assert.equal(value('.nav-item.on','color'),'var(--brand)');assert.equal(value('.brand b','color'),'var(--ink)');
  assert(!/linear-gradient|#141b2e|#101728|#4338ca/i.test(css));
});
test('native_sans_body_serif_headings_and_no_new_resource_effects',()=>{
  assert(/system|Segoe|Microsoft|PingFang/.test(value('body','font')));
  assert(/Songti SC/.test(token('--font-serif')));assert(/Georgia/.test(token('--font-serif')));
  assert.equal(value('h1','font-family'),'var(--font-serif)');
  assert(!/@import|@font-face|url\(|backdrop-filter|filter\s*:|background-image|@keyframes|animation\s*:/i.test(css));
  assert.equal(errors.length,0,errors.join('\n'));
});
test('controls_have_40_44_and_32_pixel_size_contracts',()=>{
  assert.equal(value('button','min-height'),'40px');assert.equal(value('#page-save','min-height'),'44px');
  assert.equal(value('.gate-card .btn-primary','min-height'),'44px');assert.equal(value('.btn-small','min-height'),'32px');
  assert.equal(value('.form-grid > .field > input','height'),'44px');assert.equal(value('.form-grid > .field > select','height'),'44px');
  assert.equal(value('.icon-btn','min-height'),'32px');assert.equal(value('.btn-primary','background'),'var(--brand)');
});
test('aligned_label_rows_preserve_multiline_textarea_and_values',()=>{
  assert.equal(value('.form-grid > .field','display'),'flex');assert.equal(value('.form-grid > .field > label','min-height'),'40px');
  assert.equal(value('.form-grid > .field > label','align-items'),'flex-end');
  assert.equal(value('textarea','height'),'auto');assert.equal(value('textarea','resize'),'vertical');
  assert.equal(value('textarea','min-height'),'104px');
  const textarea=dom.window.document.createElement('textarea');textarea.value='line one\nline two\nline three';
  dom.window.document.body.append(textarea);assert.equal(textarea.value.split('\n').length,3);
});
test('desktop_and_390px_mobile_rules_keep_sidebar_and_backdrop_operational',()=>{
  assert.equal(value('.sidebar','width'),'224px');assert.equal(value('.main','margin-left'),'224px');
  assert.equal(value('.main','margin-left','(max-width:900px)'),'0');
  assert.equal(value('.sidebar','transform','(max-width:900px)'),'translateX(-100%)');
  assert.equal(value('.sidebar.open','transform','(max-width:900px)'),'none');
  assert.equal(value('.sidebar-backdrop.show','display','(max-width:900px)'),'block');
  assert.equal(value('.burger','display','(max-width:900px)'),'inline-flex');
  assert.equal(value('.form-grid','grid-template-columns','(max-width:600px)'),'minmax(0,1fr)');
  assert.equal(value('.gate-card','max-width'),'100%');assert.equal(value('.content','min-width'),'0');
});
test('table_scroll_and_long_content_static_regression_anchors_remain',()=>{
  assert.equal(value('.table-wrap','overflow-x'),'auto');assert.equal(value('.table-wrap','max-width'),'100%');
  assert.equal(value('.table-wrap','min-width'),'0');assert.equal(value('table','table-layout'),'fixed');
  assert.equal(value('td','overflow-wrap'),'anywhere');assert.equal(value('.cell-preview','-webkit-line-clamp'),'3');
  assert.equal(value('.cell-full','max-height'),'240px');assert.equal(value('.cell-full','overflow-y'),'auto');
  assert.equal(value('.row-actions','flex-wrap'),'wrap');assert.equal(value('#jobs-table','min-width'),'1440px');
  assert.equal(value('#ts-table','min-width'),'1000px');assert.equal(value('.card','min-width'),'0');
});
test('semantic_green_red_amber_and_primary_text_have_readable_contrast',()=>{
  const pairs=[['body','--ink','--paper-bg'],['muted','--muted','--paper-card'],['primary','--paper-card','--brand'],
    ['success','--ok','--ok-bg'],['danger','--bad','--bad-bg'],['warning','--warn','--warn-bg']];
  observations.contrast_ratios={};for(const [label,fg,bg] of pairs){const ratio=contrast(token(fg),token(bg));observations.contrast_ratios[label]=Number(ratio.toFixed(2));assert(ratio>=4.5,label+' contrast '+ratio);}
  assert.equal(value('.badge.green','color'),'var(--ok)');assert.equal(value('.badge.red','color'),'var(--bad)');
  assert.equal(value('.badge.amber','color'),'var(--warn)');
  assert.equal(value('#mod-status-box [style*="color:#4f46e5"]','color'),'var(--brand)');
});
test('existing_administrative_script_still_parses_and_styles_have_bounded_cost',()=>{
  assert.equal(scriptMatches.length,1);new vm.Script(script,{filename:'admin.html inline script'});
  assert(!/<script[^>]*\bsrc\s*=/i.test(html));assert(!/@import|@font-face/i.test(css));
  assert(Buffer.byteLength(css)<24000,'stylesheet unexpectedly inflated');
  assert.equal(value('.sidebar','transition','(prefers-reduced-motion:reduce)'),'none');
});

const baselineFile=process.env.ADMIN_UI_BASELINE_FILE,baselineRef=process.env.ADMIN_UI_BASELINE_REF;
if(baselineFile||baselineRef){
  test('css_only_change_preserves_baseline_script_and_every_non_style_html_byte_after_checkout_normalization',()=>{
    let baseline;if(baselineFile)baseline=fs.readFileSync(path.resolve(baselineFile));else{
      assert(/^[0-9a-f]{7,40}$/i.test(baselineRef),'baseline must be a concrete commit hash');
      baseline=execFileSync('git',['show',baselineRef+':backend/admin.html'],{cwd:root,maxBuffer:8*1024*1024});
    }
    const text=baseline.toString('utf8'),baselineScripts=[...text.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)].map(x=>x[1]).join('\n');
    assert.equal(canonical(script),canonical(baselineScripts));
    const strip=text=>canonical(text.replace(/(<style\b[^>]*>)[\s\S]*?(<\/style>)/g,'$1$2'));
    assert.equal(strip(html),strip(text));
    observations.baseline_ref=baselineRef||'provided_file';observations.baseline_script_canonical_sha256=sha(canonical(baselineScripts));
    observations.baseline_script_raw_sha256=sha(baselineScripts);observations.baseline_parity_verified=true;
    observations.baseline_css_gzip_bytes=zlib.gzipSync(text.match(/<style\b[^>]*>([\s\S]*?)<\/style>/)[1]).length;
  });
}else observations.baseline_parity_verified='not requested; future script feature changes remain allowed';
if(process.env.ADMIN_UI_ORIGINAL_SCRIPT_SHA256){
  test('working_tree_raw_script_bytes_remain_exactly_frozen_for_this_style_only_change',()=>{
    assert.equal(sha(script),process.env.ADMIN_UI_ORIGINAL_SCRIPT_SHA256);
  });
}
dom.window.close();fs.mkdirSync(output,{recursive:true});
const passed=cases.filter(x=>x.passed).length;
fs.writeFileSync(path.join(output,'admin_ui_theme_results.json'),JSON.stringify({summary:{total:cases.length,passed,failed:cases.length-passed},cases,observations},null,2));
console.log(`ADMIN_UI_THEME_SUMMARY total=${cases.length} passed=${passed} failed=${cases.length-passed}`);
process.exitCode=passed===cases.length?0:1;
