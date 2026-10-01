/* Shared community action geometry and real admin cost formatting, offline. */
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const root=path.resolve(process.env.REVIEW_ROOT||path.resolve(__dirname,'../..'));
const out=path.resolve(process.env.REVIEW_OUTPUT||path.join(root,'audit/community-cost-display'));
const css=fs.readFileSync(path.join(root,'miniprogram/pages/community/community.wxss'),'utf8');
const xml=fs.readFileSync(path.join(root,'miniprogram/pages/community/community.wxml'),'utf8');
const html=fs.readFileSync(path.join(root,'backend/admin.html'),'utf8');
const rows=[];
function test(name,fn){try{fn();rows.push({case:name,passed:true});}catch(e){rows.push({case:name,passed:false,error:e.message});console.error(name,e.message);}}
test('community_actions_share_explicit_geometry_and_type',()=>{
 const body=css.match(/\.salon-actions button\s*\{([^}]+)\}/)[1];
 for(const token of ['height:80rpx','display:flex','align-items:center','justify-content:center','font-family:var(--font-serif)','font-weight:600','letter-spacing:1rpx','box-shadow:none'])assert(body.replace(/\s+/g,'').includes(token));
});
test('community_action_native_border_and_press_translation_are_normalized',()=>{
 assert(/\.salon-actions button::after\s*\{[^}]*border\s*:\s*none/.test(css));
 assert(/\.salon-actions button:active\s*\{[^}]*transform\s*:\s*none/.test(css));
});
test('community_navigation_actions_are_neutral_not_selected_filters',()=>{
 const markup=xml.match(/<view class="salon-actions">([\s\S]*?)<\/view>/)[1];
 assert.equal((markup.match(/class="salon-action"/g)||[]).length,2);
 assert(!markup.includes('btn-ink'));assert(!markup.includes('active'));
 assert(markup.includes('bindtap="onContribute"'));assert(markup.includes('bindtap="onMySubmissions"'));
 assert(markup.includes('投稿我的作品 ›'));assert(markup.includes('我的投稿 ›'));
 const body=css.match(/\.salon-actions button\s*\{([^}]+)\}/)[1].replace(/\s+/g,'');
 assert(body.includes('background:var(--paper-card-subtle)'));assert(body.includes('color:var(--ink-title)'));
 assert(css.includes('.filter-chip-active'));
});
const context={Number};vm.createContext(context);
const start=html.indexOf('function jobCostText('),end=html.indexOf('async function loadJobs()',start);
if(start>=0)vm.runInContext(html.slice(start,end),context);
test('admin_job_cost_displays_estimate_and_preserves_sub_cent_reference',()=>{
 assert.equal(context.jobCostText({cost_cny:.04,cost_estimated:true}),'≈¥0.04');
 assert.equal(context.jobCostText({cost_cny:.0048,cost_estimated:true}),'≈¥0.0048');
});
test('zero_estimated_cost_is_not_missing_and_unknown_is_not_zero',()=>{
 assert.equal(context.jobCostText({cost_cny:0,cost_estimated:true}),'≈¥0.00');
 assert.equal(context.jobCostText({cost_cny:null,cost_usd:null}),'—');
 assert.equal(context.jobCostText({}),'—');
});
test('actual_cost_and_dollar_estimates_are_distinct',()=>{
 assert.equal(context.jobCostText({cost_cny:.12,cost_estimated:false}),'¥0.12');
 assert.equal(context.jobCostText({cost_usd:.025,cost_estimated:true}),'≈$0.0250');
 assert.equal(context.jobCostText({cost_cny:'invalid'}),'—');
});
test('admin_job_table_and_daily_stats_explain_reference_cost',()=>{
 assert(html.includes('const cost = jobCostText(j)'));
 assert(html.includes('今日网关参考成本'));assert(html.includes('历史成本未记录'));
});
fs.mkdirSync(out,{recursive:true});fs.writeFileSync(path.join(out,'community_action_cost_display_results.json'),JSON.stringify({cases:rows},null,2));
const failed=rows.filter(x=>!x.passed).length;console.log(`COMMUNITY_COST_DISPLAY_SUMMARY total=${rows.length} passed=${rows.length-failed} failed=${failed}`);process.exitCode=failed?1:0;
