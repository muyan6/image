"""End-to-end drawing API/state-machine replay with an isolated COS/service double.

No paid AI generation, live WeChat calls, real account or runtime data.
"""
import json,time,hashlib,threading,unittest
from urllib.parse import urlsplit,unquote
from unittest.mock import patch
from test_cloud_pipeline import CloudTests,m,cp,OUTPUT,initial_connections
from gateway_async import AsyncImages,GatewayAsyncError


class FlowTests(CloudTests):
    def setUp(self):
        super().setUp()
        m.settings.update({'cloud_pipeline':{'audit_mode':'wechat_auto'},
          'moderation':{'enabled':True,'wechat_push_token':'fixture-token'},
          'wechat':{'app_id':'fixture','app_secret':'fixture'}})
        self.objects={};self.events=[];self.patches=[];self.trace_count=0
        def head(settings,key):
            if key not in self.objects:raise cp.cos.CosError('missing',status=404)
            return {'size':self.objects[key]['size'],'content_type':'image/jpeg'}
        def info(settings,key):head(settings,key);return {'width':1536,'height':1024}
        def copy(settings,source,target,**kwargs):
            head(settings,source);self.objects[target]=dict(self.objects[source]);self.events.append(('copy',source,target))
        def process(settings,source,target,rule):
            head(settings,source);self.objects[target]={'size':900};self.events.append(('process',target));return {'size':900}
        def audit(settings,url,openid):
            key=unquote(urlsplit(url).path.lstrip('/'));head(settings,key)
            self.trace_count+=1;trace='trace-'+str(self.trace_count);self.events.append(('wechat',key,trace));return trace
        def remove(settings,key,**kwargs):self.objects.pop(key,None);self.events.append(('delete',key))
        for target,name,fn in [(cp.cos,'object_metadata',head),(cp.cos,'image_info',info),
                               (cp.cos,'copy_object',copy),(cp.cos,'process_image',process),
                               (m.wechat_sec,'submit_image_url',audit)]:
            p=patch.object(target,name,side_effect=fn);p.start();self.patches.append(p)
        p=patch('cleanup_store.delete_object',side_effect=remove);p.start();self.patches.append(p)
        for name in ('cos_get','cos_put'):
            p=patch.object(m,name,side_effect=AssertionError('NO_HOST_IMAGE_BYTES'));p.start();self.patches.append(p)

    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        if m.cloud._audits:m.cloud._audits.close();m.cloud._audits=None
        super().tearDown()

    def upload(self,size=1000):
        r=self.client.post('/api/uploads',headers=self.headers,json={'filename':'photo.jpg','byte_size':size})
        self.assertEqual(r.status_code,200,r.text);u=r.json()
        self.assertEqual(urlsplit(u['url']).scheme,'https')
        # This dictionary assignment represents the client's direct COS PUT.
        self.objects[u['key']]={'size':size};self.events.append(('client_put_cos',u['key']))
        r=self.client.post('/api/uploads/'+u['upload_id']+'/complete',headers=self.headers)
        self.assertEqual(r.status_code,200,r.text);return u

    def submit(self,u):
        with patch.object(m,'_moderate_text_or_reject',return_value=None) as text_audit:
            r=self.client.post('/api/rescue/by-upload',headers=self.headers,json={'upload_id':u['upload_id'],'quality':'light','custom_prompt':'自然改善光线'})
        self.assertEqual(r.status_code,200,r.text);text_audit.assert_called_once()
        self.assertIn('自然改善光线',text_audit.call_args.args[0])
        return r.json()['job_id']

    def wx_pass(self,jid,stage='input',suggest='pass'):
        row=self.cloud.audits.row(jid,stage)
        sig=hashlib.sha1(''.join(sorted(['fixture-token','1','nonce'])).encode()).hexdigest()
        r=self.client.post('/api/wxpush',params={'signature':sig,'timestamp':'1','nonce':'nonce'},
                           json={'trace_id':row['trace'],'result':{'suggest':suggest,'label':100}})
        self.assertEqual(r.status_code,200,r.text)

    def start_vendor(self,jid):
        def submit(client,model,prompt,url,size,endpoint):
            key=unquote(urlsplit(url).path.lstrip('/'))
            self.assertTrue(key.startswith('norms/'));self.assertIn(key,self.objects)
            self.events.append(('vendor_submit',key));return 'imgtask_flow'
        with patch.object(AsyncImages,'submit',new=submit):self.cloud.step(jid)
        self.assertEqual(m.jobs.get(jid)['cloud_phase'],'generating')

    def vendor_done(self,jid):
        self.objects['images/fixture.png']={'size':1200}
        with patch.object(AsyncImages,'poll',return_value={'status':'completed','result':{'data':[{'url':'https://'+cp.MEDIA_HOST+'/images/fixture.png'}]}}):self.cloud.step(jid)
        self.cloud.step(jid);self.cloud.step(jid)

    def test_complete_drawing_flow_wx_upload_audit_generate_import_output_delete(self):
        u=self.upload();jid=self.submit(u)
        self.assertEqual(m.users.get_balance('sample_user'),60)
        self.step(jid,'prepare');self.assertEqual(m.jobs.get(jid)['cloud_phase'],'wait_audit')
        self.assertFalse(any(e[0]=='vendor_submit' for e in self.events))
        self.wx_pass(jid);self.cloud.step(jid);self.cloud.step(jid)
        self.start_vendor(jid);self.vendor_done(jid)
        self.assertEqual(m.jobs.get(jid)['cloud_phase'],'wait_audit')
        self.assertIsNone(m._job_media_url(m.jobs.get(jid),'result'))
        self.wx_pass(jid,'output');self.cloud.step(jid);self.cloud.step(jid)
        job=m.jobs.get(jid);self.assertEqual(job['status'],'succeeded');self.assertTrue(m._job_media_url(job,'result'))
        self.assertEqual(m.users.get_balance('sample_user'),60)
        self.assertEqual(sum(e[0]=='wechat' for e in self.events),2)
        self.assertEqual(sum(e[0]=='vendor_submit' for e in self.events),1)
        self.assertEqual((job['input_audit_engine'],job['output_audit_engine']),('wechat','wechat'))
        self.assertEqual(self.client.get('/api/jobs/'+jid,headers={'Authorization':'Bearer '+m.user_token('other_user')}).status_code,404)
        r=self.client.delete('/api/my/jobs/'+jid,headers=self.headers);self.assertEqual(r.status_code,200)
        for key in (u['key'],job['orig_cos'],job['norm_cos'],job['result_cos']):self.assertNotIn(key,self.objects)
        self.assertIsNone(m._job_media_url(m.jobs.get(jid),'result'))

    def test_large_input_uses_cos_callback_not_wechat_then_completes(self):
        u=self.upload(11*1024*1024);jid=self.submit(u);self.step(jid,'prepare')
        row=self.cloud.audits.row(jid,'input');self.assertEqual(row['engine'],'cos');self.assertEqual(self.trace_count,0)
        body={'EventName':'ReviewImage','JobsDetail':{'State':'Success','Result':0,
             'Url':'https://fixture-123456.cos.ap-guangzhou.myqcloud.com/'+row['capability']}}
        self.assertEqual(self.client.post('/api/callbacks/cos-audit',json=body).status_code,200)
        self.cloud.step(jid);self.cloud.step(jid);self.start_vendor(jid);self.vendor_done(jid)
        self.wx_pass(jid,'output');self.cloud.step(jid);self.cloud.step(jid)
        self.assertEqual(m.jobs.get(jid)['status'],'succeeded')
        self.assertEqual(m.jobs.get(jid)['input_audit_engine'],'cos')

    def test_input_rejection_stops_before_supplier(self):
        u=self.upload();jid=self.submit(u);self.step(jid,'prepare');self.wx_pass(jid,suggest='risky')
        with patch.object(AsyncImages,'submit',side_effect=AssertionError('UNREVIEWED_INPUT')):
            self.cloud.step(jid);self.cloud.step(jid)
        self.assertEqual(m.jobs.get(jid)['status'],'failed');self.assertFalse(any(e[0]=='vendor_submit' for e in self.events))

    def test_upload_permit_survives_registry_restart_and_is_single_use(self):
        u=self.upload();m._uploads.close()
        self.assertEqual(m._uploads[u['upload_id']]['key'],u['key'])
        self.submit(u)
        r=self.client.post('/api/rescue/by-upload',headers=self.headers,json={'upload_id':u['upload_id']})
        self.assertEqual(r.status_code,404)

    def test_owner_guard_and_actual_upload_size_checked_before_admission(self):
        u=self.upload()
        other={'Authorization':'Bearer '+m.user_token('other_user')}
        self.assertEqual(self.client.post('/api/uploads/'+u['upload_id']+'/complete',headers=other).status_code,404)
        self.objects[u['key']]['size']=m.MAX_UPLOAD_BYTES+1
        self.assertEqual(self.client.post('/api/uploads/'+u['upload_id']+'/complete',headers=self.headers).status_code,413)
        self.assertEqual(m.users.get_balance('sample_user'),100)
        self.objects[u['key']]['size']=999
        self.assertEqual(self.client.post('/api/uploads/'+u['upload_id']+'/complete',headers=self.headers).status_code,400)

    def test_banned_maintenance_and_pending_cap_block_upload_authorization(self):
        m.users.set_banned('sample_user',True)
        self.assertEqual(self.client.post('/api/uploads',headers=self.headers,json={'filename':'x.jpg'}).status_code,403)
        m.users.set_banned('sample_user',False);m.settings.update({'maintenance':{'enabled':True,'message':'维护'}})
        self.assertEqual(self.client.post('/api/uploads',headers=self.headers,json={'filename':'x.jpg'}).status_code,503)
        m.settings.update({'maintenance':{'enabled':False}})
        for i in range(32):m._uploads[str(i)]={'openid':'sample_user','created_at':time.time(),'key':'uploads/'+str(i),'ext':'.jpg'}
        self.assertEqual(self.client.post('/api/uploads',headers=self.headers,json={'filename':'x.jpg'}).status_code,429)

    def test_early_delete_retains_cleanup_for_late_put_until_url_expiry(self):
        u=self.upload();key=u['key']
        m.cleanup.schedule('cos',key,time.time());m.cleanup.run(m.settings)
        self.assertNotIn(key,self.objects)
        due=m.cleanup._conn.execute("SELECT due FROM cleanup WHERE kind='cos' AND target=?",(key,)).fetchone()[0]
        self.objects[key]={'size':1000}  # late PUT using the still-valid signed URL
        with patch('cleanup_store.time.time',return_value=due+1):m.cleanup.run(m.settings)
        self.assertNotIn(key,self.objects)
        self.assertIsNone(m.cleanup._conn.execute('SELECT target FROM upload_cleanup_guard WHERE target=?',(key,)).fetchone())

    def test_callback_wakeup_is_not_overwritten_by_waiter_snapshot(self):
        u=self.upload();jid=self.submit(u);self.step(jid,'prepare');a=self.cloud.audits
        row=a.row(jid,'input');read=threading.Event();attempt=threading.Event();done=threading.Event();old=a.row
        def snapshot(*args):
            value=old(*args);read.set();attempt.wait(1);done.wait(.05);return value
        def callback():
            read.wait(1);attempt.set();a.wechat_callback(row['trace'],'pass');done.set()
        t=threading.Thread(target=callback);t.start()
        with patch.object(a,'row',side_effect=snapshot):a.wait(m.jobs.get(jid))
        t.join(2);self.assertFalse(t.is_alive())
        self.assertEqual(a.row(jid,'input')['state'],'done');self.assertEqual(m.jobs.get(jid)['cloud_next_at'],0)

    def test_concurrent_submit_consumes_upload_and_charges_only_once(self):
        from concurrent.futures import ThreadPoolExecutor
        u=self.upload()
        def submit():return self.client.post('/api/rescue/by-upload',headers=self.headers,json={'upload_id':u['upload_id'],'quality':'light'}).status_code
        with patch.object(m,'_moderate_text_or_reject',return_value=None),ThreadPoolExecutor(max_workers=2) as pool:
            statuses=list(pool.map(lambda _:submit(),range(2)))
        self.assertEqual(sorted(statuses),[200,404]);self.assertEqual(m.users.get_balance('sample_user'),60)

    def test_failed_admission_restores_permit_and_expiry_is_still_enforced(self):
        u=self.upload();m.users.set_balance('sample_user',0)
        with patch.object(m,'_moderate_text_or_reject',return_value=None):
            r=self.client.post('/api/rescue/by-upload',headers=self.headers,json={'upload_id':u['upload_id'],'quality':'light'})
        self.assertEqual(r.status_code,402);m._uploads.close()
        rec=m._uploads[u['upload_id']];self.assertEqual(rec['openid'],'sample_user')
        rec['created_at']=time.time()-3601;m._uploads[u['upload_id']]=rec
        self.assertEqual(self.client.post('/api/uploads/'+u['upload_id']+'/complete',headers=self.headers).status_code,404)

    def test_bad_image_fails_before_supplier_and_refunds_reservation(self):
        u=self.upload();jid=self.submit(u)
        with patch.object(cp.cos,'image_info',side_effect=cp.cos.CosError('invalid image',code='INVALID_IMAGE_INFO')),patch.object(AsyncImages,'submit',side_effect=AssertionError('INVALID_IMAGE_SENT')):
            self.step(jid,'prepare')
        self.assertEqual(m.jobs.get(jid)['status'],'failed');self.assertEqual(m.users.get_balance('sample_user'),100)

    def test_explicit_delete_during_audit_cannot_be_revived_by_late_callback(self):
        u=self.upload();jid=self.submit(u);self.step(jid,'prepare')
        trace=self.cloud.audits.row(jid,'input')['trace']
        self.assertEqual(self.client.delete('/api/my/jobs/'+jid,headers=self.headers).status_code,200)
        self.cloud.audits.wechat_callback(trace,'pass');self.cloud.step(jid)
        job=m.jobs.get(jid);self.assertTrue(job['deleted_at']);self.assertEqual(job['status'],'failed')
        self.assertIsNone(m._job_media_url(job,'result'));self.assertEqual(m.users.get_balance('sample_user'),100)

    def test_health_reports_new_audit_policy_not_legacy_ci(self):
        with patch.object(cp.cos,'check_internal',return_value={'ok':True}):
            data=self.client.get('/api/health').json()
        self.assertEqual(data['moderation']['active_engine'],'wechat_url_or_cos_auto')
        m.settings.update({'moderation':{'enabled':False}})
        with patch.object(cp.cos,'check_internal',return_value={'ok':True}):
            self.assertEqual(self.client.get('/api/health').json()['moderation']['active_engine'],'none')

    def ready_to_submit(self):
        u=self.upload();jid=self.submit(u);self.step(jid,'prepare');self.wx_pass(jid)
        self.cloud.step(jid);self.cloud.step(jid)
        self.assertEqual(m.jobs.get(jid)['cloud_phase'],'submit')
        return jid

    def test_delete_wins_before_atomic_submit_no_vendor_request_and_refund(self):
        jid=self.ready_to_submit();original=cp.cos.presign
        def delete_before_mark(*args,**kwargs):
            url=original(*args,**kwargs)
            self.assertEqual(self.client.delete('/api/my/jobs/'+jid,headers=self.headers).status_code,200)
            return url
        with patch.object(cp.cos,'presign',side_effect=delete_before_mark),patch.object(AsyncImages,'submit') as send:
            self.cloud.step(jid)
        send.assert_not_called();self.assertEqual(m.users.get_balance('sample_user'),100)
        self.assertFalse(m.jobs.get(jid).get('submitted_at'))

    def test_delete_after_vendor_acceptance_keeps_debit_and_cleans_objects(self):
        jid=self.ready_to_submit();self.start_vendor(jid);job=m.jobs.get(jid)
        for _ in range(2):
            r=self.client.delete('/api/my/jobs/'+jid,headers=self.headers)
            self.assertEqual(r.status_code,200);self.assertEqual(r.json()['balance'],60)
        self.assertTrue(m.jobs.get(jid)['cancel_without_refund'])
        for key in (job['orig_cos'],job['norm_cos'],job['result_cos']):self.assertNotIn(key,self.objects)
        row=m.users._conn.execute('SELECT state,refunded FROM job_charges WHERE job_id=?',(jid,)).fetchone()
        self.assertEqual(tuple(row),('cancelled_charged',0))

    def test_delete_during_paid_post_late_errors_do_not_refund(self):
        for uncertain in (False,True):
            m.users.set_balance('sample_user',100);jid=self.ready_to_submit()
            def late_error(*args,**kwargs):
                self.assertTrue(m.jobs.get(jid)['submitted_at'])
                self.client.delete('/api/my/jobs/'+jid,headers=self.headers)
                raise GatewayAsyncError('fixture failure after cancellation',uncertain=uncertain)
            with patch.object(AsyncImages,'submit',side_effect=late_error):self.cloud.step(jid)
            self.cloud.fail(m.jobs.get(jid),'stale failure worker')
            self.assertEqual(m.users.get_balance('sample_user'),60)
            self.assertTrue(m.jobs.get(jid)['deleted_at']);self.assertEqual(m.jobs.get(jid)['status'],'failed')

    def test_restart_reconciles_cancelled_submitted_debit_without_refund(self):
        jid=self.ready_to_submit();self.start_vendor(jid)
        # Simulate crash between durable deletion and the charge DB settlement.
        m.jobs.delete_for_openid(jid,'sample_user')
        old=m.jobs;old._conn.close()
        m.jobs=m.JobStore(ttl=old._ttl,max_entries=old._max,db_path=old._db_path)
        self.assertEqual(m.users.get_balance('sample_user'),60)
        self.assertEqual(m.users._conn.execute('SELECT state FROM job_charges WHERE job_id=?',(jid,)).fetchone()[0],'cancelled_charged')

    def test_vendor_failure_without_user_delete_still_refunds_once(self):
        jid=self.ready_to_submit()
        with patch.object(AsyncImages,'submit',side_effect=GatewayAsyncError('fixture rejected')):self.cloud.step(jid)
        self.assertEqual(m.jobs.get(jid)['status'],'failed');self.assertEqual(m.users.get_balance('sample_user'),100)
        self.cloud.fail(m.jobs.get(jid),'duplicate failure')
        self.assertEqual(m.users.get_balance('sample_user'),100)

    def test_duplicate_paid_submission_marker_prevents_second_post(self):
        jid=self.ready_to_submit();stale=m.jobs.get(jid)
        with patch.object(AsyncImages,'submit',return_value='imgtask_once') as send:
            self.cloud.submit(stale);self.cloud.submit(stale)
        self.assertEqual(send.call_count,1)

    def test_text_to_image_checks_text_and_only_audits_output(self):
        m.settings.update({'text_generation':{'enabled':True,'base_url':'https://text.invalid','api_key':'text-key','model':'text-model','price':40}})
        with patch.object(m,'_moderate_text_or_reject',return_value=None) as text_check:
            r=self.client.post('/api/text-generation',headers=self.headers,json={'prompt':'蓝色山丘','aspect_ratio':'1:1'})
        self.assertEqual(r.status_code,200,r.text);text_check.assert_called_once_with('蓝色山丘','sample_user')
        jid=r.json()['job_id'];self.step(jid,'prepare')
        self.assertIsNone(m.jobs.get(jid)['orig_cos']);self.assertEqual(self.trace_count,0)
        def submit(client,model,prompt,url,size,endpoint):
            self.assertEqual(client.base,'https://text.invalid');self.assertIsNone(url)
            self.assertEqual(size,'1024x1024');return 'imgtask_text_flow'
        with patch.object(AsyncImages,'submit',new=submit):self.cloud.step(jid)
        self.vendor_done(jid);self.wx_pass(jid,'output');self.cloud.step(jid);self.cloud.step(jid)
        self.assertEqual(m.jobs.get(jid)['status'],'succeeded');self.assertEqual(self.trace_count,1)
        self.assertEqual(m.users.get_balance('sample_user'),60)


if __name__=='__main__':
    suite=unittest.TestSuite(FlowTests(n) for n in FlowTests.__dict__ if n.startswith('test_'))
    names=[t._testMethodName for t in suite];r=unittest.TextTestRunner(verbosity=2).run(suite)
    failed={t._testMethodName for t,_ in r.failures+r.errors}
    (OUTPUT/'drawing_flow_results.json').write_text(json.dumps({'cases':[{'case':n,'passed':n not in failed} for n in names]},indent=2),encoding='utf-8')
    m._uploads.close()
    for c in initial_connections:
        try:c.close()
        except Exception:pass
    print('DRAWING_FLOW_SUMMARY total=%d passed=%d failed=%d'%(r.testsRun,r.testsRun-len(failed),len(failed)))
    raise SystemExit(0 if r.wasSuccessful() else 1)
