"""Full-review account fixes: copied source, isolated state, no external API calls."""
from pathlib import Path
from types import SimpleNamespace
import builtins, calendar, datetime, hashlib, io, json, os, shutil, subprocess, sys, tempfile, threading, time, unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

ROOT=Path(os.environ.get('REVIEW_ROOT',Path(__file__).resolve().parents[2])).resolve()
OUTPUT=Path(os.environ.get('REVIEW_OUTPUT',ROOT/'audit/full-review-account-tests')).resolve()
OUTPUT.mkdir(parents=True,exist_ok=True)
fixture=tempfile.TemporaryDirectory(prefix='account_source_',dir=OUTPUT)
SOURCE=Path(fixture.name)/'backend';SOURCE.mkdir()
for p in (ROOT/'backend').iterdir():
    if p.is_file() and p.suffix in ('.py','.html'):shutil.copy2(p,SOURCE/p.name)
os.environ['ADMIN_PASSWORD']='full-review-account-fixture'
sys.path.insert(0,str(SOURCE))
import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
import settings_store as ss, user_store as us, admin_api as adm, templates_store as ts, cos_store as cos
network=patch.object(requests.sessions.Session,'request',side_effect=AssertionError('NETWORK_DISABLED'));network.start()
observations=[]

class AccountFixTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='state_',dir=OUTPUT);self.data=Path(self.temp.name)
        self.env=patch.dict(os.environ,DATA_DIR=str(self.data));self.env.start()
        self.users=us.UserStore(str(self.data));self.users.ensure_user('fixture-user')
        self.settings=ss.SettingsStore(str(self.data))
        self.ann=ss.AnnouncementStore(str(self.data))
        with patch.object(ts.TemplateStore,'ensure_placeholder_covers'):
            self.templates=ts.TemplateStore(str(self.data))
        self.templates.create_template({'id':'cover_fixture','group_id':'restore','name':'fixture','prompt':'fixture'})
        app=FastAPI();app.include_router(adm.make_admin_router(settings=self.settings,announcements=self.ann,
            templates=self.templates,jobs=SimpleNamespace(),users=self.users,health_fn=lambda:{},stats_fn=lambda:{}))
        self.client=TestClient(app)
        self.client.post('/admin/api/login',json={'password':'full-review-account-fixture'})
        self.headers={'X-Admin-Request':'1'}
    def tearDown(self):
        self.client.close();self.users.close();self.env.stop();self.temp.cleanup()
    def test_settings_read_io_error_preserves_valid_configuration(self):
        self.settings.update({'wechat':{'app_id':'wx-fixture','app_secret':'fixture-secret'},
            'prices':{'light':75,'fine':95},'maintenance':{'enabled':True}})
        path=Path(self.settings._path);before=path.read_bytes();real_open=builtins.open;hits=[]
        def blocked(filename,*args,**kwargs):
            mode=args[0] if args else kwargs.get('mode','r')
            if isinstance(filename,(str,bytes,os.PathLike)) and Path(filename).resolve()==path and 'r' in mode:
                hits.append(1);raise PermissionError('fixture read denied')
            return real_open(filename,*args,**kwargs)
        with patch('builtins.open',side_effect=blocked):
            with self.assertRaises(PermissionError):ss.SettingsStore(str(self.data))
        self.assertEqual(hits,[1]);self.assertEqual(path.read_bytes(),before)
        self.assertEqual(list(self.data.glob('settings.json.corrupt_*')),[])
        self.assertEqual(ss.SettingsStore(str(self.data)).snapshot(),self.settings.snapshot())
    def test_settings_bad_json_still_backs_up_and_recovers(self):
        path=Path(self.settings._path);path.write_text('{invalid',encoding='utf-8')
        restored=ss.SettingsStore(str(self.data))
        self.assertEqual(restored.prices(),{'light':40,'fine':40})
        backups=list(self.data.glob('settings.json.corrupt_*'));self.assertEqual(len(backups),1)
        self.assertEqual(backups[0].read_text(encoding='utf-8'),'{invalid')
    def test_settings_migration_write_error_preserves_valid_json(self):
        path=Path(self.settings._path);doc=self.settings.snapshot();doc.pop('pricing_revision',None)
        path.write_text(json.dumps(doc),encoding='utf-8');before=path.read_bytes();replace=os.replace
        def blocked(src,dst):
            if Path(dst)==path:raise PermissionError('fixture replace denied')
            return replace(src,dst)
        with patch.object(os,'replace',side_effect=blocked):
            with self.assertRaises(PermissionError):ss.SettingsStore(str(self.data))
        self.assertEqual(path.read_bytes(),before);self.assertEqual(list(self.data.glob('settings.json.corrupt_*')),[])
    def test_beijing_calendar_day_resets_checkin_video_and_daily_quota_on_utc_host(self):
        real_date=datetime.date
        class UTCDate(real_date):
            @classmethod
            def fromtimestamp(cls,stamp):
                dt=datetime.datetime.fromtimestamp(stamp,datetime.timezone.utc);return cls(dt.year,dt.month,dt.day)
        bj=datetime.timezone(datetime.timedelta(hours=8))
        clock=[datetime.datetime(2026,9,30,23,55,tzinfo=bj).timestamp()]
        with patch.object(us.time,'time',side_effect=lambda:clock[0]), \
             patch.object(us.time,'localtime',side_effect=lambda stamp:time.gmtime(stamp)), \
             patch.object(us.time,'mktime',side_effect=lambda parts:calendar.timegm(parts)), \
             patch.object(us.datetime,'date',UTCDate):
            self.assertTrue(self.users.claim_checkin('fixture-user',10,30)['claimed'])
            self.assertTrue(self.users.claim_video_reward('fixture-user','day1-video',10,1)[0])
            self.users.reserve_job('fixture-user','day1-job',0,{'daily':1,'per_minute':0})
            self.users.confirm_job('day1-job','fixture');self.users.complete_charge('day1-job')
            clock[0]=datetime.datetime(2026,10,1,0,5,tzinfo=bj).timestamp()
            second=self.users.claim_checkin('fixture-user',10,30)
            self.assertTrue(second['claimed']);self.assertEqual(second['checkin_streak'],2)
            self.assertEqual(self.users.earn_count_today('fixture-user','video'),0)
            self.assertTrue(self.users.claim_video_reward('fixture-user','day2-video',10,1)[0])
            self.users.reserve_job('fixture-user','day2-job',0,{'daily':1,'per_minute':0})
            reset=datetime.datetime.fromtimestamp(us._local_midnight(),bj).isoformat()
            self.assertEqual(reset,'2026-10-01T00:00:00+08:00')
            observations.append({'case':'beijing_midnight','second_checkin':second['claimed'],'reset':reset})
    def test_feedback_is_filtered_before_pagination_and_legacy_list_remains(self):
        now=time.time()
        with self.users._lock,self.users._conn:
            self.users._conn.executemany('INSERT INTO violations VALUES(?,?,?,?,?,?,?,?)',
                [('feedback-%03d'%i,'fixture-user',now-1000+i,'text','fixture',0,'active','please review') for i in range(31)]
                +[('ordinary-%03d'%i,'fixture-user',now+i,'text','fixture',0,'active','') for i in range(120)]
                +[('reviewed','fixture-user',now-1001,'text','fixture',0,'overturned','reviewed feedback')])
        ids=[]
        for offset in (0,10,20,30):
            r=self.client.get('/admin/api/violations?feedback_only=true&status=active&limit=10&offset='+str(offset),headers=self.headers)
            self.assertEqual(r.status_code,200,r.text);d=r.json()
            self.assertTrue(all(x['feedback'] and x['status']=='active' for x in d['items']))
            self.assertTrue('total' in d,'filtered violation API must expose pagination metadata')
            self.assertEqual(d['total'],31)
            ids.extend(x['id'] for x in d['items']);self.assertEqual(d['has_more'],offset<30)
        self.assertEqual(len(ids),31);self.assertEqual(len(set(ids)),31)
        all_feedback=self.client.get('/admin/api/violations?feedback_only=true&status=all',headers=self.headers).json()
        self.assertEqual(all_feedback['total'],32)
        legacy=self.client.get('/admin/api/violations?limit=5',headers=self.headers).json()
        self.assertEqual(len(legacy['items']),5);self.assertEqual(legacy['items'][0]['feedback'],'')
        self.assertEqual(len(self.users.list_violations(5)),5)
        self.assertEqual(self.client.get('/admin/api/violations?status=invalid',headers=self.headers).status_code,400)
    def test_feedback_frontend_requests_filtered_pages(self):
        html=(SOURCE/'admin.html').read_text(encoding='utf-8')
        for marker in ('feedback_only=true','id="violation-status"','id="violation-prev"','id="violation-next"','violationPage'):
            self.assertTrue(marker in html,'missing feedback page control: '+marker)
        self.assertNotIn('(data.items || []).filter(v => v.feedback)',html)
        script=r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(process.argv[1],'utf8'),start=html.indexOf('let VIOLATION_OFFSET=');
const source=html.slice(start,html.indexOf('\nasync function reviewViolation',start));
const nodes=new Map(),calls=[];
function element(){return {innerHTML:'',children:[],appendChild(x){this.children.push(x);}};}
function get(id){if(!nodes.has(id)){
 const node={value:id==='violation-status'?'active':'',disabled:false,textContent:'',tBodies:[element()]};
 Object.defineProperty(node,'innerHTML',{set(value){this.html=value;if(id==='violations-table')this.tBodies[0]=element();},get(){return this.html||'';}});
 nodes.set(id,node);
}return nodes.get(id);}
const ctx={document:{createElement:element},$:get,tableColumns:()=>'',esc:String,textCell:String,fmtTime:String,toast:m=>{throw Error(m);},api:async path=>{
 calls.push(path);const q=new URL(path,'https://fixture.invalid').searchParams;
 assert.equal(q.get('feedback_only'),'true');const offset=Number(q.get('offset')),total=q.get('status')==='active'?31:32;
 return {total,has_more:offset+30<total,items:Array.from({length:Math.max(0,Math.min(30,total-offset))},(_,i)=>({id:'feedback-'+(offset+i),openid:'fixture',feedback:'review',reason:'fixture',status:'active',created_at:1}))};
}};ctx.window=ctx;vm.createContext(ctx);vm.runInContext(source,ctx);
(async()=>{
 await ctx.loadViolationFeedback(true);assert.equal(get('violations-table').tBodies[0].children.length,30);assert(!get('violation-next').disabled);assert(get('violation-prev').disabled);
 ctx.violationPage(1);await new Promise(setImmediate);assert.equal(get('violations-table').tBodies[0].children.length,1);assert(get('violation-next').disabled);assert(!get('violation-prev').disabled);
 get('violation-status').value='all';await ctx.loadViolationFeedback(true);assert(calls.at(-1).includes('status=all'));assert(calls.at(-1).includes('offset=0'));
 console.log('FEEDBACK_FRONTEND_PAGINATION_OK');
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        result=subprocess.run(['node','-e',script,str(SOURCE/'admin.html')],capture_output=True,text=True,encoding='utf-8')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('FEEDBACK_FRONTEND_PAGINATION_OK',result.stdout)
    @staticmethod
    def image(color):
        buf=io.BytesIO();Image.new('RGB',(16,16),color).save(buf,'JPEG');return buf.getvalue()
    def cover_uploads(self,slots):
        self.settings.update({'tencent':{'secret_id':'fixture-id','secret_key':'fixture-key','cos_bucket':'fixture-123456','cos_region':'ap-guangzhou'}})
        payload={'A':self.image('red'),'B':self.image('blue')}
        hashes={k:hashlib.sha256(adm._compress_cover(v)).hexdigest() for k,v in payload.items()}
        read_barrier=threading.Barrier(2);a_put=threading.Event();b_commit=threading.Event();local=threading.local()
        objects={};uploads=[];commits=[];get=self.templates.get_template;set_cover=self.templates.set_cover_slot
        def read(tid):
            row=get(tid);read_barrier.wait(timeout=10);return row
        def put(settings,key,data):
            role=next(k for k,v in hashes.items() if v==hashlib.sha256(data).hexdigest());local.role=role
            uploads.append({'role':role,'key':key});
            if role=='A':
                objects[key]=data;a_put.set();self.assertTrue(b_commit.wait(10))
            else:self.assertTrue(a_put.wait(10));objects[key]=data
        def commit(tid,slot,ref):
            row=set_cover(tid,slot,ref);commits.append({'role':local.role,'ref':ref})
            if local.role=='B':b_commit.set()
            return row
        def upload(role):
            return self.client.post('/admin/api/templates/cover_fixture/covers?slot='+str(slots[role]),headers=self.headers,
                files={'file':(role+'.jpg',payload[role],'image/jpeg')})
        with patch.object(self.templates,'get_template',side_effect=read),patch.object(self.templates,'set_cover_slot',side_effect=commit), \
             patch.object(cos,'put_object',side_effect=put),patch.object(cos,'presign',side_effect=lambda settings,verb,key,**kw:'https://fixture.invalid/'+key):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures=[pool.submit(upload,role) for role in ('A','B')];responses=[f.result(20) for f in futures]
        self.assertEqual([r.status_code for r in responses],[200,200])
        row=get('cover_fixture')
        if slots['A']==slots['B']:
            self.assertEqual(len({u['key'] for u in uploads}),2,'uploads must never share mutable object keys')
            self.assertEqual(commits[-1]['role'],'A');key=row['cover'][4:]
            self.assertEqual(hashlib.sha256(objects[key]).hexdigest(),hashes['A'])
            self.assertEqual(hashlib.sha256((Path(ts.covers_dir())/Path(key).name).read_bytes()).hexdigest(),hashes['A'])
        else:
            self.assertEqual(len(row['covers']),2)
            for role,slot in slots.items():
                key=row['covers'][slot][4:]
                self.assertEqual(hashlib.sha256(objects[key]).hexdigest(),hashes[role])
                self.assertEqual(hashlib.sha256((Path(ts.covers_dir())/Path(key).name).read_bytes()).hexdigest(),hashes[role])
        self.assertEqual(row['cover_v'],2)
        with patch.object(ts.TemplateStore,'ensure_placeholder_covers'):
            self.assertEqual(ts.TemplateStore(str(self.data)).get_template('cover_fixture')['covers'],row['covers'])
        observations.append({'case':'cover_uploads','slots':slots,'keys':uploads,'commit_roles':[c['role'] for c in commits]})
    def test_concurrent_same_slot_cover_keeps_last_saved_image_and_backup(self):self.cover_uploads({'A':0,'B':0})
    def test_concurrent_different_cover_slots_preserve_both_updates(self):self.cover_uploads({'A':0,'B':1})
    def test_cos_failure_keeps_unique_local_cover_backup(self):
        self.settings.update({'tencent':{'secret_id':'fixture-id','secret_key':'fixture-key','cos_bucket':'fixture-123456','cos_region':'ap-guangzhou'}})
        refs=[]
        with patch.object(cos,'put_object',side_effect=cos.CosError('fixture offline')):
            for color in ('red','blue'):
                r=self.client.post('/admin/api/templates/cover_fixture/covers?slot=0',headers=self.headers,files={'file':('x.jpg',self.image(color),'image/jpeg')})
                self.assertEqual(r.status_code,200,r.text);refs.append(r.json()['cover'])
        self.assertTrue(all(ref.startswith('local:') for ref in refs));self.assertNotEqual(refs[0],refs[1])
        self.assertTrue(all((Path(ts.covers_dir())/ref[6:]).is_file() for ref in refs))

if __name__=='__main__':
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(AccountFixTests);names=[x._testMethodName for x in suite]
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    failed={x._testMethodName for x,_ in result.failures+result.errors}
    record={'total':result.testsRun,'passed':result.testsRun-len(failed),'failed':len(failed),
        'observations':observations,'cases':[{'case':n,'passed':n not in failed} for n in names]}
    (OUTPUT/'full_review_account_results.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
    network.stop();fixture.cleanup()
    print('FULL_REVIEW_ACCOUNT_SUMMARY total=%d passed=%d failed=%d'%(record['total'],record['passed'],record['failed']))
    raise SystemExit(0 if result.wasSuccessful() else 1)
