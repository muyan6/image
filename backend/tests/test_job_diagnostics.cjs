const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const root=path.resolve(process.env.REVIEW_ROOT||path.resolve(__dirname,'../..'));
const out=path.resolve(process.env.REVIEW_OUTPUT||path.join(root,'audit/job-diagnostics-tests'));fs.mkdirSync(out,{recursive:true});
const html=fs.readFileSync(path.join(root,'backend/admin.html'),'utf8');
const code=html.slice(html.indexOf('function jobPhaseText('),html.indexOf('async function loadJobs()'));
const c={Date};vm.createContext(c);vm.runInContext(code,c);const rows=[];
function test(name,fn){try{fn();rows.push({case:name,passed:true});}catch(e){console.error(e);rows.push({case:name,passed:false});}}
test('missing_timing_is_not_zero',()=>assert.equal(c.jobTimingText({status:'processing'},100),'等待阶段计时'));
test('live_ai_wait_increases',()=>assert(c.jobTimingText({status:'processing',submitted_at:58,created_at:50},100).includes('AI 已等待 42s')));
test('failed_import_phase_preserved',()=>assert.equal(c.jobPhaseText({status:'failed',failed_phase:'import'}),'成品导入 COS'));
test('audit_stage_and_new_timings_visible',()=>{assert.equal(c.jobPhaseText({status:'processing',cloud_phase:'wait_audit',audit_stage:'output'}),'成品审核');assert(c.jobTimingText({status:'succeeded',timings:{output_audit_ms:1200,import_wait_ms:3500}}).includes('成品导入 4s'));});
fs.writeFileSync(path.join(out,'job_diagnostics_results.json'),JSON.stringify({cases:rows},null,2));
const failed=rows.filter(x=>!x.passed).length;console.log(`JOB_DIAGNOSTICS_SUMMARY total=${rows.length} passed=${rows.length-failed} failed=${failed}`);process.exitCode=failed?1:0;
