const fs=require('fs'),path=require('path'),assert=require('assert'),vm=require('vm');
const root=path.resolve(process.env.REVIEW_ROOT||path.resolve(__dirname,'../..'));
const out=path.resolve(process.env.REVIEW_OUTPUT||path.join(root,'audit/compare-typography'));
const xml=fs.readFileSync(path.join(root,'miniprogram/pages/compare/compare.wxml'),'utf8');
const css=fs.readFileSync(path.join(root,'miniprogram/pages/compare/compare.wxss'),'utf8');
const rows=[];function test(name,fn){try{fn();rows.push({case:name,passed:true});}catch(e){rows.push({case:name,passed:false,error:e.message});}}
test('back_uses_css_chevron_and_literal_chinese',()=>{
 assert(xml.includes('<view class="back-chevron" aria-hidden="true"></view>'));
 assert(xml.includes('<text class="compare-label">返回</text>'));assert(!xml.includes('back-glyph'));
 assert(/\.back-chevron\s*\{[^}]*border-left:[^}]*rotate\(45deg\)/s.test(css));
});
test('chinese_controls_do_not_use_monospace_or_decorative_fonts',()=>{
 assert(!xml.includes('typewriter-label'));assert(!css.includes('var(--font-mono)'));assert(!css.includes('var(--font-serif)'));
 assert(css.includes('"PingFang SC", "Microsoft YaHei", sans-serif'));
 assert(/\.nav-back \.compare-label\s*\{[^}]*font-size: 28rpx/.test(css));
 assert(css.includes('env(safe-area-inset-bottom)'));
});
test('back_save_compare_handlers_are_unchanged_and_present',()=>{
 let p;vm.runInNewContext(fs.readFileSync(path.join(root,'miniprogram/pages/compare/compare.js'),'utf8'),{Page:x=>p=x,getApp:()=>({globalData:{}}),require:()=>({})});
 for(const m of xml.matchAll(/\b(?:bind|catch)\w+="([\w$]+)"/g))assert.equal(typeof p[m[1]],'function',m[1]);
 assert(xml.includes('bindtap="onClose"'));assert(xml.includes('bindtap="onDownload"'));assert(xml.includes('bindtouchmove="onTouchMove"'));
});
fs.mkdirSync(out,{recursive:true});fs.writeFileSync(path.join(out,'compare_typography_results.json'),JSON.stringify({cases:rows},null,2));
const failed=rows.filter(r=>!r.passed).length;console.log(`COMPARE_TYPOGRAPHY_SUMMARY total=${rows.length} passed=${rows.length-failed} failed=${failed}`);process.exitCode=failed?1:0;
