"""COS import availability and diagnostic regressions. No live generation."""
import json,time,unittest
from unittest.mock import patch
from test_cloud_pipeline import CloudTests,m,cp,OUTPUT,initial_connections
from test_cloud_origin import Fixture,response
from gateway_async import AsyncImages
import cloud_import


class ImportTests(CloudTests):
    def importing(self):
        jid=self.new();m.jobs.update(jid,cloud_phase='import',vendor_result_key='images/fixture.png',vendor_task_id='imgtask_fixture',provider_completed_at=time.time())
        return jid

    def test_pending_object_keeps_new_import_and_does_not_resubmit_ai(self):
        jid=self.importing();missing=cp.cos.CosError('pending',status=404)
        with patch.object(cp.cos,'object_metadata',side_effect=[missing,missing]) as head,patch.object(AsyncImages,'submit',side_effect=AssertionError('PAID_RESUBMIT')):
            self.cloud.step(jid)
        j=m.jobs.get(jid);self.assertEqual(j['status'],'processing');self.assertEqual(j['cloud_phase'],'import')
        self.assertTrue(j['cloud_import_triggered']);self.assertEqual(j['import_trigger_attempts'],1)
        self.assertGreater(j['cloud_next_at'],time.time());self.assertEqual(m.users.get_balance('sample_user'),60)
        cp.cos.trigger_mirror.reset_mock();self.cloud.step(jid)
        cp.cos.trigger_mirror.assert_not_called();self.assertEqual(m.jobs.get(jid)['cloud_phase'],'finalize')

    def test_existing_cached_import_does_not_trigger_another_get(self):
        jid=self.importing();self.cloud.step(jid)
        cp.cos.trigger_mirror.assert_not_called();self.assertEqual(m.jobs.get(jid)['cloud_phase'],'finalize')

    def test_424_retries_are_bounded_and_keep_diagnostics(self):
        jid=self.importing()
        missing=cp.cos.CosError('pending',status=404)
        blocked=cp.cos.CosError('COS HTTP 424 MirrorFailed source 403',status=424,code='MirrorFailed',details={'upstream_status':403})
        with patch.object(cp.cos,'object_metadata',side_effect=missing),patch.object(cp.cos,'trigger_mirror',side_effect=blocked):
            for _ in range(cloud_import.TRIGGER_LIMIT):self.cloud.step(jid)
        j=m.jobs.get(jid);self.assertEqual(j['status'],'failed');self.assertEqual(j['failed_phase'],'import')
        self.assertEqual(j['import_trigger_attempts'],cloud_import.TRIGGER_LIMIT)
        self.assertEqual(j['import_diagnostic']['upstream_status'],403);self.assertEqual(m.users.get_balance('sample_user'),100)

    def test_backfill_timeout_fails_without_delivering(self):
        jid=self.importing();m.jobs.update(jid,cloud_import_started_at=time.time()-121)
        with patch.object(cp.cos,'object_metadata',side_effect=cp.cos.CosError('missing',status=404)):
            self.cloud.step(jid)
        j=m.jobs.get(jid)
        self.assertEqual(j['status'],'failed');self.assertEqual(j['failed_phase'],'import')
        self.assertIn('等待超时',j['error']);self.assertIsNone(m._job_media_url(j,'result'))

    def test_unexpected_permission_denied_does_not_loop(self):
        jid=self.importing()
        with patch.object(cp.cos,'object_metadata',side_effect=cp.cos.CosError('denied',status=403)):
            self.cloud.step(jid)
        self.assertEqual(m.jobs.get(jid)['status'],'failed')

    def test_range_trigger_accepts_206_without_reading_picture(self):
        # setUp patches trigger_mirror; invoke the original function via a clean
        # isolated module load to exercise its actual wire contract.
        import importlib.util
        spec=importlib.util.spec_from_file_location('cos_wire_fixture',cp.cos.__file__);wire=importlib.util.module_from_spec(spec);spec.loader.exec_module(wire)
        r=response(206,body=b'IMAGE_MUST_NOT_BE_READ')
        with patch.object(wire,'control_request',return_value=r) as request:wire.trigger_mirror(Fixture(),'images/fixture.png')
        self.assertEqual(request.call_args.kwargs['headers']['Range'],'bytes=0-0');r.iter_content.assert_not_called()

    def test_error_parser_keeps_safe_code_request_id_and_source_status(self):
        r=response(424,b'<Error><Code>MirrorFailed</Code><Message>source statusCode: 403 https://x.invalid/?token=secret</Message><RequestId>req-123</RequestId></Error>')
        e=cp.cos.response_error(r,'COS 镜像导入','images/private.png',streamed=True)
        self.assertEqual(e.details['upstream_status'],403);self.assertEqual(e.details['request_id'],'req-123')
        self.assertEqual(e.details['code'],'MirrorFailed');self.assertNotIn('secret',str(e));self.assertNotIn('private.png',str(e))

    def test_head_404_identifies_object_kind(self):
        r=response(404,b'',headers={'x-cos-request-id':'request-404=='})
        e=cp.cos.response_error(r,'COS 对象校验失败','results/private.jpg')
        self.assertEqual(e.details['object_kind'],'results');self.assertEqual(e.status,404)
        self.assertIn('request-404==',str(e))

    def test_accepted_but_stalled_backfill_is_retriggered_without_ai(self):
        jid=self.importing();now=time.time()
        m.jobs.update(jid,cloud_import_triggered=True,import_trigger_attempts=1,cloud_import_started_at=now-30,import_last_trigger_at=now-30)
        with patch.object(cp.cos,'object_metadata',side_effect=cp.cos.CosError('not stored',status=404)),patch.object(AsyncImages,'submit',side_effect=AssertionError('PAID_RESUBMIT')):
            self.cloud.step(jid)
        cp.cos.trigger_mirror.assert_called_once()
        job=m.jobs.get(jid);self.assertEqual(job['status'],'processing');self.assertEqual(job['import_trigger_attempts'],2)
        cp.cos.trigger_mirror.reset_mock();self.cloud.step(jid)
        cp.cos.trigger_mirror.assert_not_called();self.assertEqual(m.jobs.get(jid)['cloud_phase'],'finalize')

    def test_no_retrigger_before_interval_or_after_limit(self):
        jid=self.importing();now=time.time()
        m.jobs.update(jid,cloud_import_triggered=True,import_trigger_attempts=1,cloud_import_started_at=now-10,import_last_trigger_at=now-1)
        with patch.object(cp.cos,'object_metadata',side_effect=cp.cos.CosError('pending',status=404)):
            self.cloud.step(jid)
            cp.cos.trigger_mirror.assert_not_called()
            m.jobs.update(jid,import_trigger_attempts=cloud_import.TRIGGER_LIMIT,import_last_trigger_at=now-30)
            self.cloud.step(jid)
            cp.cos.trigger_mirror.assert_not_called()
        self.assertEqual(m.jobs.get(jid)['status'],'processing')

    def test_restart_preserves_backfill_attempt_and_trigger_time(self):
        jid=self.importing();now=time.time()
        m.jobs.update(jid,cloud_import_triggered=True,import_trigger_attempts=2,cloud_import_started_at=now-30,import_last_trigger_at=now-1)
        db=m.jobs._db_path;m.jobs._conn.close();m.jobs=m.JobStore(2592000,5000,db_path=db)
        with patch.object(cp.cos,'object_metadata',side_effect=cp.cos.CosError('pending',status=404)):
            self.cloud.step(jid)
        cp.cos.trigger_mirror.assert_not_called()
        self.assertEqual(m.jobs.get(jid)['import_trigger_attempts'],2)

    def test_retrigger_error_survives_later_head_errors(self):
        jid=self.importing();now=time.time()
        m.jobs.update(jid,cloud_import_triggered=True,import_trigger_attempts=1,cloud_import_started_at=now-30,import_last_trigger_at=now-30)
        with patch.object(cp.cos,'object_metadata',side_effect=cp.cos.CosError('HEAD pending',status=404)),patch.object(cp.cos,'trigger_mirror',side_effect=cp.cos.CosError('mirror source denied',status=424,details={'request_id':'origin-request'})):
            self.cloud.step(jid)
            self.cloud.step(jid)
        j=m.jobs.get(jid)
        self.assertEqual(j['import_trigger_diagnostic']['request_id'],'origin-request')
        self.assertIn('mirror source denied',j['import_last_trigger_error'])
        self.assertIn('HEAD pending',j['import_last_error'])

    def test_supplier_failed_reason_is_kept_redacted_and_refunded_once(self):
        secret='sensitive-key-123456'
        m.settings.update({'providers':{'worldcodes':{'api_key':secret}}})
        jid=self.importing();m.jobs.update(jid,cloud_phase='generating')
        data={'status':'failed','http_status':429,'result':{'error':{'code':'rate_limit','message':'Too many requests '+secret+' https://private.invalid/image?token=abc'}},'request_id':'vendor-req-1'}
        with patch.object(AsyncImages,'poll',return_value=data),patch.object(AsyncImages,'submit',side_effect=AssertionError('PAID_RESUBMIT')):
            self.cloud.step(jid);self.cloud.step(jid)
        j=m.jobs.get(jid);self.assertEqual(j['status'],'failed');self.assertEqual(j['failed_phase'],'generating')
        self.assertEqual(j['vendor_failure']['code'],'rate_limit');self.assertEqual(j['vendor_failure']['http_status'],429)
        self.assertEqual(j['vendor_failure']['task_id'],'imgtask_fixture')
        self.assertIn('Too many requests',j['error']);self.assertNotIn(secret,json.dumps(j['vendor_failure'])+j['error'])
        self.assertNotIn('private.invalid',j['error']);self.assertEqual(m.users.get_balance('sample_user'),100)

    def test_supplier_missing_reason_is_not_invented(self):
        from gateway_async import failure_diagnostic
        d=failure_diagnostic({'status':'failed'})
        self.assertFalse(d['reason_available']);self.assertEqual(d['message'],'');self.assertEqual(d['code'],'')


if __name__=='__main__':
    suite=unittest.TestSuite(ImportTests(n) for n in ImportTests.__dict__ if n.startswith('test_'))
    names=[t._testMethodName for t in suite];r=unittest.TextTestRunner(verbosity=2).run(suite)
    failed={t._testMethodName for t,_ in r.failures+r.errors}
    (OUTPUT/'cloud_import_results.json').write_text(json.dumps({'cases':[{'case':n,'passed':n not in failed} for n in names]},indent=2),encoding='utf-8')
    for c in initial_connections:
        try:c.close()
        except Exception:pass
    print('CLOUD_IMPORT_SUMMARY total=%d passed=%d failed=%d'%(r.testsRun,r.testsRun-len(failed),len(failed)))
    raise SystemExit(0 if r.wasSuccessful() else 1)
