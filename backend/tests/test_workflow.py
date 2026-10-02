"""Focused media, template framing and admin-list cleanup regressions. No live data/network."""
from pathlib import Path
import io, json, os, shutil, sqlite3, sys, tempfile, time, unittest
from unittest.mock import patch

ROOT = Path(os.environ.get('REVIEW_ROOT', Path(__file__).resolve().parents[2])).resolve()
OUTPUT = Path(os.environ.get('REVIEW_OUTPUT', ROOT / 'audit/workflow-tests')).resolve()
OUTPUT.mkdir(parents=True, exist_ok=True)
fixture = tempfile.TemporaryDirectory(prefix='workflow_', dir=OUTPUT)
base = Path(fixture.name)
source = base / 'backend'; source.mkdir()
for p in (ROOT / 'backend').glob('*'):
    if p.is_file() and p.suffix in ('.py', '.html'): shutil.copy2(p, source / p.name)
for key in ('FAL_KEY','BAIDU_API_KEY','BAIDU_SECRET_KEY','WX_APPID','WX_APP_SECRET','TENCENT_SECRET_ID','TENCENT_SECRET_KEY'):
    os.environ.pop(key, None)
os.environ.update(ADMIN_PASSWORD='workflow-tests', LOG_LEVEL='CRITICAL', DATA_DIR=str(source/'data'), UPLOAD_DIR=str(source/'uploads'))
sys.path.insert(0, str(source))
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
import requests, templates_store, admin_api
from user_store import UserStore
from settings_store import SettingsStore
from cleanup_store import CleanupStore
guard = patch.object(requests.sessions.Session, 'request', side_effect=AssertionError('NETWORK_DISABLED')); guard.start()
with patch.object(templates_store.TemplateStore, 'ensure_placeholder_covers'):
    import main as m
initial_connections = [m.users._conn, m.jobs._conn, m.cleanup._conn]

class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.d=base/self._testMethodName;self.d.mkdir()
        def fixture_defaults(doc):
            doc['processing']['ci_enabled']=False
            doc['cloud_pipeline']['enabled']=False
            # Provision an isolated compatible gateway so unrelated ownership,
            # framing and credit tests do not depend on retired local fallback.
            doc['providers']['worldcodes'].update(base_url='https://fixture.invalid',api_key='fixture',request_mode='sync')
        m.settings=SettingsStore(str(self.d),mutate_default=fixture_defaults);m.users=UserStore(str(self.d))
        m.jobs=m.JobStore(2592000,5000,db_path=str(self.d/'jobs.db'))
        m.cleanup=CleanupStore(str(self.d),m.UPLOAD_DIR)
        m.users.ensure_user('sample_user');m.users.ensure_user('other_user')
        self.headers={'Authorization':'Bearer '+m.user_token('sample_user')}
        self.client=TestClient(m.app)
        app=FastAPI();app.include_router(admin_api.make_admin_router(settings=m.settings,announcements=m.announcements,
            templates=m.templates,jobs=m.jobs,users=m.users,health_fn=m.health,stats_fn=lambda:{}))
        self.admin=TestClient(app);self.admin.post('/admin/api/login',json={'password':'workflow-tests'})
        self.admin.headers.update({'X-Admin-Request':'1'})
    def tearDown(self):
        m._uploads.close()
        if m.cloud._audits:
            m.cloud._audits.close();m.cloud._audits=None
        self.client.close();self.admin.close();m.users.close();m.jobs._conn.close();m.cleanup.close()
    def image(self, size=(160,90)):
        f=io.BytesIO();Image.new('RGB',size,'blue').save(f,'JPEG');return f.getvalue()
    def job(self, age=0, original_age=0, **extra):
        jid='abcdef123456';name='result_'+jid+'.jpg';orig='orig_'+jid+'.jpg'
        Path(m.UPLOAD_DIR,name).write_bytes(self.image());Path(m.UPLOAD_DIR,orig).write_bytes(self.image())
        m.jobs.create(jid,openid='sample_user',orig_file=orig,result_file=name,quality='light')
        m.jobs.update(jid,status='succeeded',completed_at=time.time()-age,created_at=time.time()-original_age,**extra)
        return jid
    def test_user_quality_controls_both_template_and_restore(self):
        self.assertEqual(m._chosen_quality('',''),'light');self.assertEqual(m._chosen_quality('light','fine'),'light')
        self.assertEqual(m._chosen_quality('fine','light'),'fine')

    def test_new_economy_and_credit_package_catalog(self):
        self.assertEqual(m.users.get_balance('sample_user'),100)
        self.assertEqual(m.settings.prices(),{'light':40,'fine':40})
        self.assertEqual(m.settings.rewards()['invite'],40)
        r=self.client.get('/api/credits/packages');self.assertEqual(r.status_code,200)
        items=r.json()['packages']
        self.assertEqual([(x['yuan'],x['points']) for x in items],[(6,600),(30,3300),(68,8160),(128,16640)])
        self.assertFalse(r.json()['payment_ready'])

    def test_seven_day_checkin_is_atomic_and_awards_configured_bonus(self):
        now=time.time()
        for days_ago in range(1,7):
            m.users._conn.execute("INSERT INTO audit(ts,openid,action,detail) VALUES(?,?,?,?)",
                                  (now-days_ago*86400,'sample_user','earn_checkin','+10'))
        m.users._conn.commit()
        before=self.client.get('/api/me',headers=self.headers).json()['earn']
        self.assertEqual((before['checkin_streak'],before['checkin_day']),(6,7))
        claimed=self.client.post('/api/me/earn',headers=self.headers,json={'kind':'checkin'})
        self.assertEqual(claimed.status_code,200,claimed.text)
        self.assertEqual((claimed.json()['reward'],claimed.json()['bonus_awarded']),(40,30))
        self.assertEqual(m.users.get_balance('sample_user'),140)
        self.assertEqual(self.client.post('/api/me/earn',headers=self.headers,json={'kind':'checkin'}).status_code,429)

    def test_invite_reward_and_nickname_follow_server_settings(self):
        m.settings.update({'rewards':{'invite':45,'community_featured':65}})
        code=m.users.get_user('other_user')['invite_code']
        result=self.client.post('/api/me/invite',headers=self.headers,json={'code':code})
        self.assertEqual((result.status_code,result.json()['reward'],result.json()['balance']),(200,45,145))
        self.assertEqual(m.users.get_balance('other_user'),145)
        self.assertEqual(self.client.get('/api/config').json()['rewards']['community_featured'],65)
        with patch.object(m,'_moderate_text_or_reject',return_value=None):
            profile=self.client.post('/api/me/profile',headers=self.headers,json={'nickname':'小山'})
        self.assertEqual(profile.status_code,200,profile.text)
        self.assertEqual(self.client.get('/api/me',headers=self.headers).json()['nickname'],'小山')

    def test_old_pricing_migrates_once_without_changing_other_settings(self):
        old=self.d/'old-pricing';old.mkdir()
        legacy=SettingsStore(str(old))
        legacy.update({'prices':{'light':1,'fine':3},'free_mode':True})
        path=old/'settings.json';doc=json.loads(path.read_text(encoding='utf-8'))
        doc.pop('pricing_revision',None);path.write_text(json.dumps(doc),encoding='utf-8')
        migrated=SettingsStore(str(old))
        self.assertEqual(migrated.prices(),{'light':40,'fine':40})
        self.assertTrue(migrated.free_mode())
        self.assertEqual(SettingsStore(str(old)).prices(),{'light':40,'fine':40})

    def test_existing_template_prices_migrate_to_unified_40(self):
        directory=self.d/'old-templates';directory.mkdir()
        with patch.object(templates_store.TemplateStore,'ensure_placeholder_covers'):
            store=templates_store.TemplateStore(str(directory))
            path=directory/'templates.json';doc=json.loads(path.read_text(encoding='utf-8'))
            doc['version']=2
            for item in doc['templates']:item['price']=4
            path.write_text(json.dumps(doc,ensure_ascii=False),encoding='utf-8')
            migrated=templates_store.TemplateStore(str(directory))
        self.assertTrue(all(item['price']==40 for item in migrated.list_templates()))
        self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['version'],3)

    def test_admin_price_change_updates_existing_template_prices(self):
        directory=self.d/'repriced-templates';directory.mkdir()
        with patch.object(templates_store.TemplateStore,'ensure_placeholder_covers'):
            store=templates_store.TemplateStore(str(directory))
        app=FastAPI();app.include_router(admin_api.make_admin_router(settings=m.settings,
            announcements=m.announcements,templates=store,jobs=m.jobs,users=m.users,
            health_fn=m.health,stats_fn=lambda:{}))
        with TestClient(app) as admin:
            admin.post('/admin/api/login',json={'password':'workflow-tests'})
            admin.headers.update({'X-Admin-Request':'1'})
            saved=admin.put('/admin/api/settings',json={'prices':{'light':55,'fine':55}})
        self.assertEqual(saved.status_code,200,saved.text)
        self.assertEqual(m.settings.prices(),{'light':55,'fine':55})
        self.assertTrue(all(t['price']==55 for t in store.list_templates()))
        self.assertEqual(self.client.get('/api/credits/packages').json()['generation_cost'],55)

    def test_payment_credentials_remain_admin_only_and_masked(self):
        saved=self.admin.put('/admin/api/settings',json={'payment':{
            'offer_id':'123456','sandbox_app_key':'sandbox-secret-1234',
            'production_app_key':'production-secret-5678'}})
        self.assertEqual(saved.status_code,200,saved.text)
        self.assertEqual(saved.json()['payment']['production_app_key'],'••••5678')
        self.assertEqual(m.settings.snapshot()['payment']['production_app_key'],'production-secret-5678')
        public=self.client.get('/api/config').json()
        self.assertNotIn('payment',public)
        self.assertNotIn('production-secret-5678',json.dumps(public))
    def test_multipart_default_quality(self):
        with patch.object(m.pool,'submit'):
            r=self.client.post('/api/rescue',headers=self.headers,files={'image':('x.jpg',self.image(),'image/jpeg')})
        self.assertEqual(r.status_code,200,r.text);self.assertEqual(r.json()['quality'],'light')
    def test_template_submission_keeps_uncropped_input(self):
        p=self.d/'input.jpg';p.write_bytes(self.image())
        tpl={'id':'fixture','name':'长图','engine':'fine'}
        with patch.object(m.pool,'submit') as submit,patch.object(m.templates,'inc_usage'),patch.object(m,'_get_client',return_value=type('Client',(),{'configured':True})()):
            result=m._register_job('sample_user','fine','',str(p),'.jpg',template=tpl,aspect_ratio='9:16')
        job=m.jobs.get(result['job_id'])
        self.assertEqual(result['aspect_ratio'],'');self.assertEqual(submit.call_args.args[-1],'')
        with Image.open(Path(m.UPLOAD_DIR,job['orig_file'])) as img:self.assertEqual(img.size,(160,90))
    def pipeline(self, template):
        jid=self.job();m.jobs.update(jid,status='processing')
        class Provider:
            configured=True
            def enhance(s,src,dst,**kw):
                with Image.open(src) as im:self.assertEqual(im.width/im.height,160/90)
                Path(dst).write_bytes(self.image((90,160)))
        with patch.object(m.settings,'chain',return_value=['worldcodes']),patch.object(m.settings,'provider_enabled',return_value=True),\
             patch.object(m,'_get_client',return_value=Provider()),patch.object(m,'_moderate_or_reject',return_value=None):
            m._run_pipeline(jid,'fine','',template=template,aspect_ratio='9:16' if template else '')
        job=m.jobs.get(jid);self.assertEqual(job['status'],'succeeded',job.get('error'));return job
    def test_template_model_portrait_is_not_cropped_to_landscape(self):
        job=self.pipeline({'id':'fixture','name':'长图','gateway_size':'90x160'})
        self.assertEqual((job['width'],job['height']),(90,160))
    def test_plain_restore_still_preserves_input_ratio(self):
        job=self.pipeline(None);self.assertAlmostEqual(job['width']/job['height'],160/90,delta=0.03)
    def test_expired_signed_url_and_fresh_metadata_signature(self):
        jid=self.job();job=m.jobs.get(jid);name=job['result_file'];expiry=int(time.time())-1
        signed='/api/images/'+name+'?expires='+str(expiry)+'&sig='+m._media_signature(job,name,expiry)
        self.assertEqual(self.client.get(signed).status_code,403)
        r=self.client.get('/api/jobs/'+jid,headers=self.headers)
        self.assertEqual(r.status_code,200,r.text);self.assertEqual(self.client.get(r.json()['result_url']).status_code,200)
    def test_cos_status_request_only_signs_no_picture_bytes(self):
        jid=self.job(result_cos='results/fixture.jpg')
        with patch.object(m.settings,'cos_ready',return_value=True),patch.object(m,'cos_get',side_effect=AssertionError('no proxy')),\
             patch.object(m,'cos_presign',return_value='https://cos.invalid/result?signature=fresh') as sign:
            r=self.client.get('/api/jobs/'+jid,headers=self.headers)
        self.assertEqual(r.status_code,200);self.assertEqual(r.json()['result_url'],'https://cos.invalid/result?signature=fresh')
        self.assertLessEqual(sign.call_args.kwargs['ttl_seconds'],600);self.assertLess(len(r.content),4000)
    def test_cos_only_metadata_does_not_download_to_server(self):
        jid=self.job(result_cos='results/fixture.jpg');Path(m.UPLOAD_DIR,m.jobs.get(jid)['result_file']).unlink()
        with patch.object(m.settings,'cos_ready',return_value=True),patch.object(m,'cos_get',side_effect=AssertionError('no proxy')),\
             patch.object(m,'cos_presign',return_value='https://cos.invalid/result'):
            r=self.client.get('/api/jobs/'+jid,headers=self.headers)
        self.assertEqual(r.status_code,200);self.assertEqual(r.json()['result_url'],'https://cos.invalid/result')
    def test_missing_cos_result_recovery_is_internal_only_and_returns_json(self):
        jid=self.job(result_cos='results/old.jpg');stored=set()
        def put(settings,key,data,**kw):
            self.assertTrue(kw['internal_only']);self.assertEqual(data,self.image());stored.add(key)
        with patch.object(m.settings,'cos_ready',return_value=True),patch.object(m,'cos_head',side_effect=lambda s,k:k in stored),\
             patch.object(m,'cos_put',side_effect=put),patch.object(m,'cos_presign',return_value='https://cos.invalid/recovered.jpg'):
            r=self.client.post('/api/jobs/'+jid+'/refresh-media',headers=self.headers)
        self.assertEqual(r.status_code,200,r.text);self.assertTrue(r.json()['recovered']);self.assertEqual(r.headers['content-type'],'application/json')
        self.assertLess(len(r.content),500);self.assertIn('_recovered_',m.jobs.get(jid)['result_cos'])
        due=m.cleanup._conn.execute("SELECT due FROM cleanup WHERE target=?",(m.jobs.get(jid)['result_cos'],)).fetchone()[0]
        self.assertGreater(due,time.time()+m.JOB_TTL_SECONDS-30)
    def test_recovery_never_downloads_missing_cos_picture_or_proxies_to_client(self):
        jid=self.job(result_cos='results/old.jpg');Path(m.UPLOAD_DIR,m.jobs.get(jid)['result_file']).unlink()
        with patch.object(m.settings,'cos_ready',return_value=True),patch.object(m,'cos_head',return_value=False),\
             patch.object(m,'cos_get',side_effect=AssertionError('no get')),patch.object(m,'cos_put') as put:
            r=self.client.post('/api/jobs/'+jid+'/refresh-media',headers=self.headers)
        self.assertEqual(r.status_code,404);put.assert_not_called()
    def test_recovery_auth_expiry_and_already_stored_idempotence(self):
        jid=self.job(result_cos='results/existing.jpg');url='/api/jobs/'+jid+'/refresh-media'
        self.assertEqual(self.client.post(url).status_code,401)
        other={'Authorization':'Bearer '+m.user_token('other_user')};self.assertEqual(self.client.post(url,headers=other).status_code,404)
        with patch.object(m.settings,'cos_ready',return_value=True),patch.object(m,'cos_head',return_value=True),patch.object(m,'cos_put') as put:
            r=self.client.post(url,headers=self.headers);self.assertEqual(r.status_code,200);self.assertFalse(r.json()['recovered']);put.assert_not_called()
        m.jobs.update(jid,completed_at=time.time()-m.JOB_TTL_SECONDS-1)
        self.assertEqual(self.client.post(url,headers=self.headers).status_code,410)
    def test_internal_repair_failure_does_not_use_public_upload(self):
        import cos_store
        with patch.object(cos_store,'presign',return_value='https://internal.invalid/object') as sign,\
             patch.object(cos_store.requests,'put',side_effect=requests.ConnectionError('internal unavailable')) as put:
            with self.assertRaises(cos_store.CosError):cos_store.put_object(m.settings,'key',b'fixture',internal_only=True)
        self.assertEqual(put.call_count,1);self.assertEqual(sign.call_args.kwargs['internal'],True)
    def test_internal_endpoint_suffix_and_private_dns_probe(self):
        import cos_store
        conf={'secret_id':'fixture','secret_key':'fixture','cos_bucket':'test-123','cos_region':'ap-shanghai','cos_custom_domain':'custom.invalid'}
        self.assertEqual(cos_store._host(conf,True),'test-123.cos-internal.ap-shanghai.tencentcos.cn')
        self.assertEqual(cos_store._host(conf,False),'custom.invalid')
        with patch.object(cos_store,'_conf',return_value=conf),patch('socket.gethostbyname',return_value='198.18.0.1'):
            self.assertFalse(cos_store.check_internal(m.settings)['ok'])
        with patch.object(cos_store,'_conf',return_value=conf),patch('socket.gethostbyname',return_value='100.64.0.10'):
            self.assertTrue(cos_store.check_internal(m.settings)['ok'])
    def test_cos_upload_failure_is_failed_and_refunded_not_fake_success(self):
        jid=self.job(result_cos='results/fixture.jpg');m.jobs.update(jid,status='processing')
        m.users.reserve_job('sample_user',jid,3,{});m.users.confirm_job(jid,'job='+jid)
        provider=type('FixtureGateway',(),{'configured':True,'enhance':lambda obj,src,out,**kw:Path(out).write_bytes(self.image())})()
        with patch.object(m.settings,'cos_ready',return_value=True),patch.object(m,'_get_client',return_value=provider),\
             patch.object(m,'cos_put',side_effect=m.CosError('fixture failure')) as put,patch.object(m,'cos_head',return_value=False),\
             patch.object(m,'_moderate_or_reject',return_value=None):
            m._run_pipeline(jid,'light','')
        self.assertEqual(m.jobs.get(jid)['status'],'failed');self.assertIn('结果保存到 COS 失败',m.jobs.get(jid)['error'])
        self.assertEqual(m.users.get_balance('sample_user'),100)
        self.assertEqual(len([c for c in put.call_args_list if c.args[1].startswith('results/')]),2)
        self.assertIsNone(m._job_media_url(m.jobs.get(jid),'result'))
    def test_origin_upload_failure_does_not_skip_result_key(self):
        p=self.d/'input.jpg';p.write_bytes(self.image())
        with patch.object(m.settings,'cos_ready',return_value=True),patch.object(m.pool,'submit'),\
             patch.object(m,'cos_put',side_effect=m.CosError('origin failed')):
            result=m._register_job('sample_user','light','',str(p),'.jpg')
        job=m.jobs.get(result['job_id']);self.assertIsNone(job['orig_cos']);self.assertTrue(job['result_cos'].startswith('results/'))
    def test_metadata_ownership_state_and_retention(self):
        jid=self.job();url='/api/jobs/'+jid
        self.assertEqual(self.client.get(url).status_code,401)
        other={'Authorization':'Bearer '+m.user_token('other_user')}
        self.assertEqual(self.client.get(url,headers=other).status_code,404)
        self.assertEqual(self.client.get('/api/jobs/'+jid+'/media/result',headers=self.headers).status_code,404)
        m.jobs.update(jid,status='processing');self.assertIsNone(self.client.get(url,headers=self.headers).json()['result_url'])
        m.jobs.update(jid,status='succeeded',completed_at=time.time()-m.JOB_TTL_SECONDS-1)
        self.assertIsNone(self.client.get(url,headers=self.headers).json()['result_url'])
    def test_original_expiry_remains_explicit(self):
        jid=self.job(original_age=m.ORIGINAL_TTL_SECONDS+1,age=m.JOB_TTL_SECONDS+1)
        self.assertEqual(self.client.get('/api/jobs/'+jid,headers=self.headers).json()['orig_url'],None)
    def test_admin_delete_only_removes_management_entry(self):
        jid=self.job();before=m.users.get_user('sample_user');m.users.add_balance('sample_user',7)
        r=self.admin.delete('/admin/api/users/sample_user');self.assertEqual(r.status_code,200,r.text)
        self.assertNotIn('sample_user',[u['openid'] for u in m.users.list_users()])
        self.assertEqual(m.users.stats()['users_total'],1)
        u=m.users.get_user('sample_user');self.assertFalse(u['banned']);self.assertEqual(u['balance'],107)
        self.assertEqual(u['invite_code'],before['invite_code']);self.assertIsNotNone(m.jobs.get(jid))
        self.assertEqual(self.client.get('/api/me',headers=self.headers).status_code,200)
        self.assertIn('sample_user',[u['openid'] for u in m.users.list_users()]);self.assertEqual(m.users.stats()['users_total'],2)
        self.assertEqual(m.users.get_user('sample_user')['balance'],107)
    def test_hidden_user_login_preserves_identity_and_no_second_gift(self):
        old=m.users.get_user('sample_user');m.users.set_balance('sample_user',12);m.users.set_banned('sample_user',True)
        self.assertEqual(self.admin.delete('/admin/api/users/sample_user').status_code,200)
        row=m.users.ensure_user('sample_user');self.assertEqual(row['balance'],12);self.assertTrue(row['banned'])
        self.assertEqual(row['user_id'],old['user_id']);self.assertEqual(row['admin_hidden'],False)
        self.assertIn('sample_user',[u['openid'] for u in m.users.list_users()])
    def test_delete_admin_auth_csrf_and_missing(self):
        with TestClient(self.admin.app) as anonymous:
            self.assertEqual(anonymous.delete('/admin/api/users/sample_user').status_code,403)
        self.assertEqual(self.admin.delete('/admin/api/users/missing').status_code,404)
        self.admin.headers.pop('X-Admin-Request');self.assertEqual(self.admin.delete('/admin/api/users/sample_user').status_code,403)
    def test_hidden_flag_is_durable_but_old_schema_migrates_visible(self):
        legacy=self.d/'legacy';legacy.mkdir();con=sqlite3.connect(legacy/'users.db')
        con.execute('CREATE TABLE users(openid TEXT PRIMARY KEY,created_at REAL NOT NULL,last_seen REAL NOT NULL,total_jobs INTEGER NOT NULL DEFAULT 0,blocked INTEGER NOT NULL DEFAULT 0)')
        con.execute('INSERT INTO users(openid,created_at,last_seen) VALUES(?,?,?)',('legacy',time.time(),time.time()));con.commit();con.close()
        store=UserStore(str(legacy))
        try:
            self.assertFalse(store.get_user('legacy')['admin_hidden']);self.assertEqual(len(store.list_users()),1)
            store.hide_from_admin('legacy')
        finally:store.close()
        store=UserStore(str(legacy))
        try:self.assertEqual(store.list_users(),[]);store.ensure_user('legacy');self.assertEqual(len(store.list_users()),1)
        finally:store.close()

    def test_upload_violation_deducts_and_bans_on_third_with_feedback_review(self):
        with patch.object(m, '_moderate_or_reject', return_value='图片内容未通过安全审核'):
            results = [self.client.post('/api/rescue', headers=self.headers,
                         files={'image': ('photo.jpg', self.image(), 'image/jpeg')},
                         data={'quality': 'light'}) for _ in range(3)]
        self.assertEqual([r.status_code for r in results], [422, 422, 422])
        ids = [r.json()['detail']['violation_id'] for r in results]
        self.assertEqual([r.json()['detail']['weekly_count'] for r in results], [1, 2, 3])
        self.assertTrue(results[-1].json()['detail']['banned'])
        self.assertEqual(m.users.get_balance('sample_user'), 0)
        self.assertEqual(m.users.get_user('sample_user')['blocked'], 3)
        refused = self.client.post('/api/rescue', headers=self.headers,
            files={'image': ('photo.jpg', self.image(), 'image/jpeg')})
        self.assertEqual(refused.status_code, 403)
        feedback = self.client.post('/api/me/violation-feedback', headers=self.headers,
                                    json={'violation_id': ids[-1], 'message': '照片误判，请复核'})
        self.assertEqual(feedback.status_code, 200)
        listed = self.admin.get('/admin/api/violations').json()['items']
        self.assertEqual(listed[0]['feedback'], '照片误判，请复核')
        reviewed = self.admin.post('/admin/api/violations/' + ids[-1] + '/review',
                                   json={'accepted': True})
        self.assertEqual(reviewed.json()['balance'], 20)
        self.assertFalse(m.users.get_user('sample_user')['banned'])

    def test_custom_prompt_and_template_text_moderated_before_registration(self):
        with patch.object(m, '_moderate_or_reject', return_value=None), \
             patch.object(m, '_moderate_text_or_reject', return_value=None) as moderate, \
             patch.object(m, '_register_job', return_value={'code': 0, 'job_id': 'sample'}) as register:
            result = self.client.post('/api/rescue', headers=self.headers,
                files={'image': ('photo.jpg', self.image(), 'image/jpeg')},
                data={'quality': 'fine', 'custom_prompt': '移除背景人群，保留主体'})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(register.call_args.kwargs['custom_prompt'], '移除背景人群，保留主体')
        self.assertEqual(moderate.call_args.args[0], '移除背景人群，保留主体')

    def test_custom_prompt_never_silently_uses_non_prompt_engine(self):
        with patch.object(m, '_moderate_or_reject', return_value=None), \
             patch.object(m, '_moderate_text_or_reject', return_value=None), \
             patch.object(m.settings, 'gateway_candidates', return_value=[]):
            before = m.users.get_balance('sample_user')
            result = self.client.post('/api/rescue', headers=self.headers,
                files={'image': ('photo.jpg', self.image(), 'image/jpeg')},
                data={'custom_prompt': '改变人物动作'})
        self.assertEqual(result.status_code, 503)
        self.assertEqual(m.users.get_balance('sample_user'), before)

    def test_user_delete_immediately_removes_own_cos_objects(self):
        jid=self.job(orig_cos='origins/sample/photo.jpg',result_cos='results/sample/photo.jpg',
                     norm_cos='norms/sample/photo.jpg')
        with patch.object(m.settings,'cos_ready',return_value=True), \
             patch('cleanup_store.delete_object',return_value=None) as delete:
            result=self.client.delete('/api/my/jobs/'+jid,headers=self.headers)
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual((result.json()['cos_deleted'],result.json()['cos_pending']),(3,0))
        self.assertEqual(delete.call_count,3)
        self.assertEqual(m.cleanup._conn.execute("SELECT COUNT(*) FROM cleanup WHERE kind='cos' AND target LIKE '%sample/%'").fetchone()[0],0)
        self.assertFalse(Path(m.UPLOAD_DIR,'result_'+jid+'.jpg').exists())

    def test_user_delete_cos_failure_keeps_durable_retry(self):
        jid=self.job(result_cos='results/sample/retry.jpg')
        with patch.object(m.settings,'cos_ready',return_value=True), \
             patch('cleanup_store.delete_object',side_effect=RuntimeError('offline')):
            result=self.client.delete('/api/my/jobs/'+jid,headers=self.headers)
        self.assertEqual(result.status_code,200)
        self.assertEqual(result.json()['cos_pending'],1)
        self.assertEqual(m.cleanup._conn.execute(
            "SELECT COUNT(*) FROM cleanup WHERE kind='cos' AND target='results/sample/retry.jpg'").fetchone()[0],1)

    def test_text_violation_and_service_failure_have_different_billing(self):
        with patch.object(m, '_moderate_text_or_reject', return_value='文字内容未通过安全审核'):
            blocked = self.client.post('/api/rescue', headers=self.headers,
                files={'image': ('photo.jpg', self.image(), 'image/jpeg')},
                data={'custom_prompt': '测试文字'})
        self.assertEqual(blocked.status_code, 422)
        self.assertEqual(blocked.json()['detail']['charged'], 40)
        before = m.users.get_balance('sample_user')
        with patch.object(m, '_moderate_text_or_reject', side_effect=m.HTTPException(503, '审核服务不可用')):
            failed = self.client.post('/api/rescue', headers=self.headers,
                files={'image': ('photo.jpg', self.image(), 'image/jpeg')},
                data={'custom_prompt': '测试文字'})
        self.assertEqual(failed.status_code, 503)
        self.assertEqual(m.users.get_balance('sample_user'), before)

    def test_admin_bulk_remove_preserves_accounts_ledgers_and_works(self):
        m.users.audit('sample_user','test_record','fixture')
        m.users.record_violation('sample_user','v123456','text','fixture',1)
        m.jobs.create('keptjob12345',openid='sample_user',status='succeeded',quality='light')
        original=m.users.get_user('sample_user')
        preview=self.admin.get('/admin/api/users/remove-summary')
        self.assertEqual(preview.status_code,200)
        self.assertEqual(preview.json()['accounts'],2)
        stale=self.admin.request('DELETE','/admin/api/users',json={'confirmation':'移出全部用户','expected_accounts':1})
        self.assertEqual(stale.status_code,409)
        self.assertEqual(len(m.users.list_users(200,account_type='all')),2)
        result=self.admin.request('DELETE','/admin/api/users',json={'confirmation':'移出全部用户','expected_accounts':2})
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()['removed_from_list'],2)
        self.assertEqual(m.users.list_users(200,account_type='all'),[])
        kept=m.users.get_user('sample_user')
        self.assertEqual((kept['balance'],kept['user_id']),(original['balance'],original['user_id']))
        self.assertIsNotNone(m.jobs.get('keptjob12345'))
        self.assertEqual(m.users._conn.execute('SELECT COUNT(*) FROM violations').fetchone()[0],1)
        self.assertEqual(self.client.get('/api/me',headers=self.headers).status_code,200)
        self.assertEqual(len(m.users.list_users(200,account_type='all')),1)
        self.assertEqual(m.users.get_balance('sample_user'),original['balance'])

    def test_admin_bulk_remove_keeps_running_job_and_rejects_legacy_delete(self):
        m.jobs.create('activejob1234',openid='sample_user',quality='light')
        old=self.admin.request('DELETE','/admin/api/users',json={'confirmation':'删除全部账号','expected_accounts':2,'expected_jobs':1})
        self.assertEqual(old.status_code,400)
        result=self.admin.request('DELETE','/admin/api/users',json={'confirmation':'移出全部用户','expected_accounts':2})
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(m.jobs.get('activejob1234')['status'],'processing')
        self.assertIsNotNone(m.users.get_user('sample_user'))
        html=(ROOT/'backend/admin.html').read_text(encoding='utf-8')
        self.assertIn('全部移出列表',html);self.assertNotIn('永久删除全部账号',html)

if __name__=='__main__':
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(WorkflowTests);names=[t._testMethodName for t in suite]
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    failed={t._testMethodName for t,_ in result.failures+result.errors}
    (OUTPUT/'workflow_results.json').write_text(json.dumps({'cases':[{'case':name,'passed':name not in failed} for name in names]},indent=2),encoding='utf-8')
    print('WORKFLOW_SUMMARY total=%d passed=%d failed=%d'%(result.testsRun,result.testsRun-len(failed),len(failed)))
    m.pool.shutdown(wait=True)
    for con in initial_connections:con.close()
    guard.stop();fixture.cleanup();sys.exit(0 if result.wasSuccessful() else 1)
