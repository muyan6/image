"""Isolated cloud scheduling, billing and crash-resume regressions. No model calls."""
import copy,json,time,unittest
from pathlib import Path
from unittest.mock import patch,Mock
from test_workflow import WorkflowTests,m,OUTPUT,initial_connections
from gateway_async import AsyncImages,GatewayAsyncError
from image_processing import QueueFull
import cloud_pipeline as cp
from cloud_layout import text_rule

class CloudTests(WorkflowTests):
    def setUp(self):
        super().setUp()
        m.settings.update({'cloud_pipeline':{'enabled':True,'audit_mode':'ci_sync'},'quota':{'daily':1000,'per_minute':1000},
                           'tencent':{'secret_id':'fixture','secret_key':'fixture','cos_bucket':'fixture-123456','cos_region':'ap-guangzhou'},
                           'providers':{'worldcodes':{'enabled':True,'base_url':'https://fixture.invalid','api_key':'fixture','request_mode':'async'}}})
        self.cloud=m.cloud;self.cloud.busy.clear()
        self.ready=patch.object(self.cloud,'ready',return_value=True);self.ready.start()
        self.source={'key':'incoming/owned.jpg','ext':'.jpg','openid':'sample_user','created_at':time.time()}
        self.stack=[]
        for name,rv in [('object_metadata',{'size':1000,'content_type':'image/jpeg'}),
                        ('image_info',{'width':1536,'height':1024}),('copy_object',None),
                        ('process_image',None),('trigger_mirror',None)]:
            p=patch.object(cp.cos,name,return_value=rv);self.stack.append(p);p.start()
    def tearDown(self):
        for p in self.stack:p.stop()
        self.ready.stop();super().tearDown()
    def new(self,**kwargs):return self.cloud.admit('sample_user',source=self.source,**kwargs)['job_id']
    def step(self,jid,phase):
        m.jobs.update(jid,cloud_phase=phase);self.cloud.step(jid);return m.jobs.get(jid)
    def generating(self):
        jid=self.new();self.step(jid,'prepare')
        with patch.object(AsyncImages,'submit',return_value='imgtask_fixture'):
            self.step(jid,'submit')
        return jid
    def completed(self,jid):
        with patch.object(AsyncImages,'poll',return_value={'status':'completed','result':{'data':[{'url':'https://'+cp.MEDIA_HOST+'/images/test.png'}]}}):
            self.step(jid,'generating')
        self.step(jid,'import');return self.step(jid,'finalize')

    def test_cloud_photo_no_host_image_reads_or_writes(self):
        with (patch.object(m,'cos_get',side_effect=AssertionError('NO_HOST_READ')),
              patch.object(m,'cos_put',side_effect=AssertionError('NO_HOST_WRITE')),
              patch.object(m,'_validate_image',side_effect=AssertionError('NO_HOST_DECODE'))):
            jid=self.generating();job=self.completed(jid)
        self.assertEqual(job['status'],'succeeded',job.get('error'))
        self.assertEqual(job['orig_file'],'');self.assertEqual(job['result_file'],'')
        self.assertEqual(job['processing_mode'],'cloud_only');self.assertEqual(m.users.get_balance('sample_user'),60)
        self.assertTrue(job['comparison_cos']);self.assertIsNone(job.get('vendor_result_url'))

    def test_cloud_submission_timeout_never_retries_paid_post(self):
        jid=self.new();self.step(jid,'prepare')
        with patch.object(AsyncImages,'submit',side_effect=GatewayAsyncError('timeout',uncertain=True)) as submit:
            self.step(jid,'submit');self.cloud.step(jid);self.cloud.step(jid)
        submit.assert_called_once();self.assertEqual(m.jobs.get(jid)['cloud_phase'],'unknown')
        self.assertEqual(m.users.get_balance('sample_user'),60)
        m.jobs.update(jid,deadline=time.time()-1);self.cloud.step(jid)
        self.assertEqual(m.users.get_balance('sample_user'),100)

    def test_cloud_restart_recovers_metadata_and_remote_task_without_refund(self):
        jid=self.generating();m.jobs.update(jid,timings={'provider_ms':123},comparison_cos='norms/fixture.jpg')
        db=m.jobs._db_path;m.jobs._conn.close();m.jobs=m.JobStore(2592000,5000,db_path=db)
        job=m.jobs.get(jid)
        self.assertEqual(job['status'],'processing');self.assertEqual(job['vendor_task_id'],'imgtask_fixture')
        self.assertEqual(job['comparison_cos'],'norms/fixture.jpg');self.assertEqual(job['timings']['provider_ms'],123)
        self.assertNotIn('api_key',job['cloud_request']);self.assertEqual(m.users.get_balance('sample_user'),60)
        with patch.object(AsyncImages,'submit',side_effect=AssertionError('RESUBMIT')):self.completed(jid)
        self.assertEqual(m.jobs.get(jid)['status'],'succeeded')

    def test_cloud_restart_submitting_is_unknown_not_resubmitted(self):
        jid=self.new();m.jobs.update(jid,cloud_phase='submitting')
        db=m.jobs._db_path;m.jobs._conn.close();m.jobs=m.JobStore(2592000,5000,db_path=db)
        with patch.object(AsyncImages,'submit',side_effect=AssertionError('RESUBMIT')):self.cloud.step(jid)
        self.assertEqual(m.jobs.get(jid)['cloud_phase'],'unknown')

    def test_cloud_generation_wait_is_not_a_metadata_worker(self):
        m.settings.update({'free_mode':True})
        ids=[self.new() for _ in range(20)]
        class Immediate:
            def submit(_,fn,jid):fn(jid)
        old=self.cloud.executor;self.cloud.executor=Immediate()
        try:
            with patch.object(AsyncImages,'submit',return_value='imgtask_fixture'):
                self.cloud.tick();self.cloud.tick()
            s=self.cloud.snapshot()
            self.assertEqual(s['remote_active'],16);self.assertEqual(s['queued'],4)
            self.assertEqual(s['metadata_workers'],4);self.assertEqual(len(self.cloud.busy),0)
        finally:self.cloud.executor=old

    def test_cloud_full_queue_rejects_before_charge(self):
        m.settings.update({'cloud_pipeline':{'generation_concurrency':1,'max_queued':0}})
        self.new()
        with self.assertRaises(m.HTTPException) as e:self.new()
        self.assertEqual(e.exception.status_code,429);self.assertEqual(m.users.get_balance('sample_user'),60)

    def test_cloud_preflight_rejects_before_reserve(self):
        with patch.object(self.cloud,'ready',return_value=False):
            with self.assertRaises(m.HTTPException):self.new()
        self.assertEqual(m.users.get_balance('sample_user'),100)

    def test_cloud_vendor_rejection_and_failure_refund_once(self):
        jid=self.new();self.step(jid,'prepare')
        with patch.object(AsyncImages,'submit',side_effect=GatewayAsyncError('unsupported model')):
            self.step(jid,'submit');self.cloud.step(jid)
        self.assertEqual(m.jobs.get(jid)['status'],'failed');self.assertEqual(m.users.get_balance('sample_user'),100)

    def test_cloud_poll_transient_error_retries_get_not_post(self):
        jid=self.generating()
        with patch.object(AsyncImages,'poll',side_effect=GatewayAsyncError('timeout')):
            self.step(jid,'generating')
        self.assertEqual(m.jobs.get(jid)['status'],'processing');self.assertGreater(m.jobs.get(jid)['cloud_next_at'],time.time())

    def test_cloud_vendor_result_host_or_base64_rejected(self):
        jid=self.generating()
        with patch.object(AsyncImages,'poll',return_value={'status':'completed','result':{'data':[{'url':'https://other.invalid/images/x.png'}]}}):
            self.step(jid,'generating')
        self.assertEqual(m.jobs.get(jid)['status'],'failed');self.assertEqual(m.users.get_balance('sample_user'),100)
        self.assertIsNone(m._job_media_url(m.jobs.get(jid),'result'))

    def test_cloud_output_audit_blocks_delivery_and_refunds(self):
        jid=self.generating()
        m.settings.update({'moderation':{'enabled':True,'block_on_error':True}})
        with patch.object(cp.cos,'audit_object',return_value={'result':2,'label':'review'}):
            job=self.completed(jid)
        self.assertEqual(job['status'],'failed');self.assertEqual(m.users.get_balance('sample_user'),100)
        self.assertIsNone(m._job_media_url(job,'result'))

    def test_cloud_strict_audit_outage_never_passes(self):
        jid=self.new();m.settings.update({'moderation':{'enabled':True,'block_on_error':True}})
        with patch.object(cp.cos,'audit_object',side_effect=cp.cos.CosError('unavailable')):
            job=self.step(jid,'prepare')
        self.assertEqual(job['status'],'failed');self.assertEqual(m.users.get_balance('sample_user'),100)

    def test_cloud_input_violation_records_only_one_penalty(self):
        jid=self.new();m.settings.update({'moderation':{'enabled':True,'block_on_error':True}})
        with patch.object(cp.cos,'audit_object',return_value={'result':1,'label':'block'}):
            self.step(jid,'prepare');self.cloud.step(jid)
        self.assertEqual(m.users.get_balance('sample_user'),60)
        self.assertEqual(m.jobs.get(jid)['status'],'failed')

    def test_cloud_cancellation_during_copy_never_delivers(self):
        jid=self.generating()
        with patch.object(cp.cos,'process_image',side_effect=lambda *a:m.jobs.delete_for_openid(jid,'sample_user')):
            self.completed(jid)
        self.assertIsNone(m._job_media_url(m.jobs.get(jid),'result'))

    def test_cloud_result_stays_independent_of_shared_import(self):
        jid=self.generating();job=self.completed(jid)
        self.assertNotEqual(job['result_cos'],job['vendor_result_key'])
        self.assertEqual(job['vendor_result_key'],'images/test.png')
        self.assertIn('results/',job['result_cos'])

    def test_cloud_text_job_uses_same_queue_and_has_no_original(self):
        m.settings.update({'text_generation':{'enabled':True,'model':'fixture','base_url':'https://text.invalid','api_key':'text-fixture'}})
        r=self.cloud.admit('sample_user',text={'prompt':'水彩森林','model':'fixture','endpoint':'/v1/images/generations','size':'1024x1024','price':40})
        jid=r['job_id'];self.step(jid,'prepare')
        with patch.object(AsyncImages,'submit',return_value='imgtask_fixture') as submit:self.step(jid,'submit')
        self.assertEqual(submit.call_args.args[2],None);self.assertEqual(submit.call_args.args[4],'/v1/images/generations')
        job=self.completed(jid);self.assertEqual(job['input_mode'],'text');self.assertIsNone(m._job_media_url(job,'orig'))

    def test_cloud_template_keeps_input_uncropped_and_global_tier(self):
        template={'id':'fixture','name':'模板','prompt':'纵向海报','engine':'fine','model_override':'old','price':1}
        jid=self.new(template=template,quality='light',aspect_ratio='9:16')
        self.step(jid,'prepare');job=m.jobs.get(jid)
        self.assertEqual(job['aspect_ratio'],'');self.assertEqual(job['price'],40)
        with patch.object(AsyncImages,'submit',return_value='imgtask_fixture') as submit:self.step(jid,'submit')
        self.assertEqual(submit.call_args.args[3],'1024x1536');self.assertEqual(submit.call_args.args[0],m.settings.provider('worldcodes')['model_light'])

    def test_cloud_by_upload_is_owned_and_does_not_read_picture(self):
        m._uploads['fixture']={**self.source}
        with patch.object(m,'cos_get',side_effect=AssertionError('HOST_READ')):
            response=self.client.post('/api/rescue/by-upload',headers=self.headers,json={'upload_id':'fixture','quality':'light'})
        self.assertEqual(response.status_code,200,response.text)
        self.assertTrue(m.jobs.get(response.json()['job_id'])['cloud_pipeline'])
        self.assertEqual(self.client.post('/api/rescue/by-upload',headers=self.headers,json={'upload_id':'fixture'}).status_code,404)

    def test_cloud_multipart_does_not_fallback_to_host(self):
        r=self.client.post('/api/rescue',headers=self.headers,files={'image':('sample.jpg',self.image(),'image/jpeg')})
        self.assertEqual(r.status_code,503);self.assertEqual(m.users.get_balance('sample_user'),100)

    def test_cloud_geometry_metadata_or_oversize_failure_refunds(self):
        jid=self.new()
        with patch.object(cp.cos,'object_metadata',return_value={'size':m.MAX_UPLOAD_BYTES+1}):job=self.step(jid,'prepare')
        self.assertEqual(job['status'],'failed');self.assertEqual(m.users.get_balance('sample_user'),100)

    def test_cloud_text_layout_is_ci_metadata_not_ai_text(self):
        template={'layout':'postcard_bottom','text_fields':[{'key':'title','role':'title'}]}
        rule=text_rule(1536,1024,template,{'title':'自然山川'})
        self.assertIn('watermark/2/text/',rule);self.assertNotIn('自然山川',rule);self.assertIn('/font/',rule)

    def test_cloud_sweeper_does_not_register_comparison_with_24h_expiry(self):
        jid=self.new();m._startup_file_gc()
        job=m.jobs.get(jid)
        due=m.cleanup._conn.execute("SELECT due FROM cleanup WHERE kind='cos' AND target=?",(job['norm_cos'],)).fetchone()[0]
        self.assertGreater(due,job['created_at']+m.JOB_TTL_SECONDS)
        self.step(jid,'prepare')
        with patch.object(AsyncImages,'submit',return_value='imgtask_fixture'):self.step(jid,'submit')
        job=self.completed(jid)
        due=m.cleanup._conn.execute("SELECT due FROM cleanup WHERE kind='cos' AND target=?",(job['norm_cos'],)).fetchone()[0]
        self.assertGreaterEqual(due,job['completed_at']+m.JOB_TTL_SECONDS-.1)

    def test_cloud_completed_extras_survive_restart_and_original_expiry(self):
        jid=self.generating();self.completed(jid)
        m.jobs.update(jid,created_at=time.time()-90000)
        db=m.jobs._db_path;m.jobs._conn.close();m.jobs=m.JobStore(2592000,5000,db_path=db)
        self.assertTrue(m._job_media_url(m.jobs.get(jid),'orig'))
        self.assertEqual(m.jobs.get(jid)['input_mode'],'photo')

    def test_cloud_legacy_upload_rejected_before_body_read(self):
        import asyncio
        async def run():
            messages=[]
            async def receive():raise AssertionError('MUST_NOT_READ_IMAGE_BODY')
            async def send(message):messages.append(message)
            await m.app({'type':'http','asgi':{'version':'3.0'},'http_version':'1.1','method':'POST',
                         'scheme':'https','path':'/api/rescue','raw_path':b'/api/rescue',
                         'query_string':b'','headers':[(b'content-type',b'multipart/form-data; boundary=fixture')],
                         'client':('fixture',1),'server':('fixture',443)},receive,send)
            self.assertEqual(messages[0]['status'],503)
        asyncio.run(run())

    def test_cloud_audit_permission_failure_is_not_silently_bypassed(self):
        jid=self.new();m.settings.update({'moderation':{'enabled':True,'block_on_error':False}})
        with patch.object(cp.cos,'audit_object',side_effect=cp.cos.CosError('403')):
            job=self.step(jid,'prepare')
        self.assertEqual(job['status'],'failed');self.assertEqual(m.users.get_balance('sample_user'),100)

if __name__=='__main__':
    suite=unittest.TestSuite(CloudTests(n) for n in CloudTests.__dict__ if n.startswith('test_cloud_'))
    names=[t._testMethodName for t in suite];result=unittest.TextTestRunner(verbosity=2).run(suite)
    failed={t._testMethodName for t,_ in result.failures+result.errors}
    (OUTPUT/'cloud_pipeline_results.json').write_text(json.dumps({'cases':[{'case':n,'passed':n not in failed} for n in names]},indent=2),encoding='utf-8')
    for c in initial_connections:
        try:c.close()
        except Exception:pass
    print('CLOUD_PIPELINE_SUMMARY total=%d passed=%d failed=%d'%(result.testsRun,result.testsRun-len(failed),len(failed)))
    raise SystemExit(0 if result.wasSuccessful() else 1)
