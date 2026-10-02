"""Offline cloud callback regressions; no credentials, network or real billing."""
import json
import time
import unittest
from unittest.mock import patch
from test_cloud_pipeline import CloudTests, m, cp, OUTPUT, initial_connections
from cloud_audit import CloudAudit


class AuditTests(CloudTests):
    def setUp(self):
        super().setUp()
        m.settings.update({'cloud_pipeline':{'audit_mode':'wechat_auto'},
                           'moderation':{'enabled':True,'block_on_error':True,'wechat_push_token':'fixture'},
                           'wechat':{'app_id':'fixture','app_secret':'fixture'}})
        self.submit_patch=patch.object(m.wechat_sec,'submit_image_url',return_value='trace-input')
        self.submit=self.submit_patch.start()

    def tearDown(self):
        self.submit_patch.stop()
        if self.cloud._audits:
            self.cloud._audits.close();self.cloud._audits=None
        super().tearDown()

    def waiting(self,size=1000):
        jid=self.new()
        with patch.object(cp.cos,'object_metadata',return_value={'size':size}):
            self.step(jid,'prepare')
        return jid

    def wake(self,jid):
        self.cloud.step(jid)
        self.cloud.step(jid)
        return m.jobs.get(jid)

    def body(self,jid,result=0,stage='input',detail=True):
        row=self.cloud.audits.row(jid,stage)
        url='https://fixture-123456.cos.ap-guangzhou.myqcloud.com/'+row['capability']
        if detail:
            return {'EventName':'ReviewImage','JobsDetail':{'Url':url,'State':'Success','Result':result}}
        return {'code':0,'data':{'event':'ReviewImage','url':url,'result':result}}

    def test_wechat_link_only_nonblocking_and_pass(self):
        with (patch.object(cp.cos,'audit_object',side_effect=AssertionError('ACTIVE_CI')),
              patch.object(m,'cos_get',side_effect=AssertionError('HOST_READ'))):
            jid=self.waiting()
        self.assertEqual(m.jobs.get(jid)['cloud_phase'],'wait_audit')
        self.submit.assert_called_once()
        self.assertIn(m.jobs.get(jid)['orig_cos'],self.submit.call_args.args[1])
        self.cloud.audits.wechat_callback('trace-input','pass')
        self.assertEqual(self.wake(jid)['cloud_phase'],'submit')

    def test_ten_mb_boundary_and_above_routes_cos(self):
        jid=self.waiting(10*1024*1024)
        self.assertEqual(self.cloud.audits.row(jid,'input')['engine'],'wechat')
        self.submit.reset_mock()
        jid=self.waiting(10*1024*1024+1)
        self.assertEqual(self.cloud.audits.row(jid,'input')['engine'],'cos')
        self.submit.assert_not_called()
        cp.cos.copy_object.assert_called_with(m.settings,m.jobs.get(jid)['orig_cos'],self.cloud.audits.row(jid,'input')['capability'],private=True)

    def test_cos_detail_simple_and_duplicate_callback(self):
        for detail in (True,False):
            with patch.object(m.wechat_sec,'wechat_sec_ready',return_value=False):jid=self.waiting()
            body=self.body(jid,detail=detail)
            r=self.client.post('/api/callbacks/cos-audit',json=body)
            self.assertEqual(r.status_code,200)
            self.assertTrue(self.cloud.audits.cos_callback(self.body(jid,result=1,detail=detail)))
            self.assertEqual(self.wake(jid)['cloud_phase'],'submit')

    def test_wechat_reject_never_falls_back_and_penalizes_once(self):
        jid=self.waiting();self.cloud.audits.wechat_callback('trace-input','risky')
        self.wake(jid);self.cloud.step(jid)
        self.assertEqual(m.jobs.get(jid)['status'],'failed')
        self.assertIsNone(self.cloud.audits.row(jid,'input')['capability'])
        self.assertEqual(m.users.get_balance('sample_user'),60)

    def test_wechat_error_and_timeout_fallback_to_cos(self):
        self.submit.side_effect=m.WechatSecError('network',code='NETWORK')
        jid=self.waiting();self.assertEqual(self.cloud.audits.row(jid,'input')['engine'],'cos')
        self.submit.side_effect=None
        jid=self.waiting()
        self.cloud.audits.update(self.cloud.audits.row(jid,'input')['id'],deadline=time.time()-1)
        self.wake(jid)
        self.assertEqual(self.cloud.audits.row(jid,'input')['engine'],'cos')
        self.assertEqual(m.jobs.get(jid)['status'],'processing')

    def test_early_callback_and_restart_are_durable(self):
        def early(*args):
            self.cloud.audits.wechat_callback('trace-early','pass');return 'trace-early'
        self.submit.side_effect=early
        jid=self.waiting()
        self.cloud.audits.close();self.cloud._audits=CloudAudit(self.cloud.runtime)
        self.assertEqual(self.wake(jid)['cloud_phase'],'submit')

    def test_unknown_missing_and_error_verdict_never_pass(self):
        with patch.object(m.wechat_sec,'wechat_sec_ready',return_value=False):jid=self.waiting()
        body=self.body(jid)
        body['JobsDetail']['Url']=body['JobsDetail']['Url'].replace('fixture-123456','other-123456')
        self.assertFalse(self.cloud.audits.cos_callback(body))
        body=self.body(jid);del body['JobsDetail']['Result']
        self.assertFalse(self.cloud.audits.cos_callback(body))
        body=self.body(jid);body['JobsDetail']['State']='Failed'
        self.assertTrue(self.cloud.audits.cos_callback(body))
        self.assertEqual(self.wake(jid)['status'],'failed')
        self.assertEqual(m.users.get_balance('sample_user'),100)

    def test_cos_callback_missing_times_out_and_refunds(self):
        with patch.object(m.wechat_sec,'wechat_sec_ready',return_value=False):jid=self.waiting()
        row=self.cloud.audits.row(jid,'input');self.cloud.audits.update(row['id'],deadline=time.time()-1)
        self.cloud.step(jid)
        self.assertEqual(m.jobs.get(jid)['status'],'failed')
        self.assertEqual(m.users.get_balance('sample_user'),100)

    def test_output_audit_waits_without_reprocessing_or_delivery(self):
        m.settings.update({'text_generation':{'enabled':True,'model':'fixture','base_url':'https://text.invalid','api_key':'text-fixture'}})
        # Prepare without input; output still needs its own audit.
        from gateway_async import AsyncImages
        jid=self.cloud.admit('sample_user',text={'prompt':'fixture','model':'fixture','price':40})['job_id']
        self.step(jid,'prepare')
        with patch.object(AsyncImages,'submit',return_value='fixture-task'):self.step(jid,'submit')
        self.submit.return_value='trace-output'
        job=self.completed(jid)
        self.assertEqual(job['cloud_phase'],'wait_audit')
        self.assertIsNone(m._job_media_url(job,'result'))
        n=cp.cos.process_image.call_count
        self.cloud.audits.wechat_callback('trace-output','pass')
        job=self.wake(jid)
        self.assertEqual(job['status'],'succeeded')
        self.assertEqual(cp.cos.process_image.call_count,n)

    def test_signed_wechat_endpoint_and_invalid_signature(self):
        import hashlib
        jid=self.waiting()
        body={'trace_id':'trace-input','result':{'suggest':'pass','label':100}}
        self.assertEqual(self.client.post('/api/wxpush',json=body).status_code,403)
        signature=hashlib.sha1(''.join(sorted(['fixture','1','nonce'])).encode()).hexdigest()
        r=self.client.post('/api/wxpush',params={'timestamp':'1','nonce':'nonce','signature':signature},json=body)
        self.assertEqual(r.status_code,200)
        self.assertEqual(self.wake(jid)['cloud_phase'],'submit')

    def test_console_probe_and_oversized_body(self):
        self.assertEqual(self.client.post('/api/callbacks/cos-audit',json={'message':'probe'}).status_code,200)
        self.assertEqual(self.client.post('/api/callbacks/cos-audit',content=b'x'*262145).status_code,413)
        self.assertEqual(self.client.post('/api/callbacks/cos-audit',content=b'bad').status_code,400)

    def test_input_snapshot_is_not_recopied_after_audit(self):
        jid=self.waiting()
        self.assertEqual(cp.cos.copy_object.call_count,1)
        self.cloud.audits.wechat_callback('trace-input','pass')
        self.wake(jid)
        self.assertEqual(cp.cos.copy_object.call_count,1)
        self.assertEqual(self.cloud.audits.row(jid,'input')['source'],m.jobs.get(jid)['orig_cos'])

    def test_pending_cos_survives_restart_and_cancel_ignores_callback(self):
        with patch.object(m.wechat_sec,'wechat_sec_ready',return_value=False):jid=self.waiting()
        body=self.body(jid)
        self.cloud.audits.close();self.cloud._audits=CloudAudit(self.cloud.runtime)
        self.assertEqual(self.cloud.audits.row(jid,'input')['state'],'waiting')
        m.jobs.delete_for_openid(jid,'sample_user')
        self.assertTrue(self.cloud.audits.cos_callback(body))
        self.assertEqual(self.cloud.audits.row(jid,'input')['state'],'waiting')

    def test_output_cos_reject_refunds_without_user_penalty(self):
        m.settings.update({'text_generation':{'enabled':True,'model':'fixture','base_url':'https://text.invalid','api_key':'text-fixture'}})
        from gateway_async import AsyncImages
        jid=self.cloud.admit('sample_user',text={'prompt':'fixture','model':'fixture','price':40})['job_id']
        self.step(jid,'prepare')
        with patch.object(AsyncImages,'submit',return_value='fixture-task'):self.step(jid,'submit')
        with patch.object(m.wechat_sec,'wechat_sec_ready',return_value=False):self.completed(jid)
        self.assertTrue(self.cloud.audits.cos_callback(self.body(jid,result=2,stage='output')))
        self.assertEqual(self.wake(jid)['status'],'failed')
        self.assertEqual(m.users.get_balance('sample_user'),100)

    def test_early_wechat_error_falls_back_not_pass(self):
        def early(*args):
            self.cloud.audits.wechat_callback('early-error','error');return 'early-error'
        self.submit.side_effect=early
        jid=self.waiting()
        self.assertEqual(self.cloud.audits.row(jid,'input')['engine'],'cos')
        self.assertEqual(self.cloud.audits.row(jid,'input')['state'],'waiting')

    def test_wechat_failure_callback_falls_back_immediately(self):
        jid=self.waiting()
        self.cloud.audits.wechat_callback('trace-input','error')
        self.wake(jid)
        row=self.cloud.audits.row(jid,'input')
        self.assertEqual(row['engine'],'cos');self.assertEqual(row['state'],'waiting')
        self.assertGreater(row['deadline'],time.time()+250)

    def test_copy_crash_resume_checks_existing_object_not_duplicate_copy(self):
        with patch.object(m.wechat_sec,'wechat_sec_ready',return_value=False):jid=self.waiting()
        row=self.cloud.audits.row(jid,'input');self.cloud.audits.update(row['id'],state='copying')
        cp.cos.copy_object.reset_mock()
        self.step(jid,'prepare')
        cp.cos.copy_object.assert_not_called()
        self.assertEqual(self.cloud.audits.row(jid,'input')['state'],'waiting')


if __name__=='__main__':
    suite=unittest.TestSuite(AuditTests(n) for n in AuditTests.__dict__ if n.startswith('test_'))
    names=[t._testMethodName for t in suite];result=unittest.TextTestRunner(verbosity=2).run(suite)
    failed={t._testMethodName for t,_ in result.failures+result.errors}
    (OUTPUT/'cloud_audit_results.json').write_text(json.dumps({'cases':[{'case':n,'passed':n not in failed} for n in names]},indent=2),encoding='utf-8')
    for c in initial_connections:
        try:c.close()
        except Exception:pass
    print('CLOUD_AUDIT_SUMMARY total=%d passed=%d failed=%d'%(result.testsRun,result.testsRun-len(failed),len(failed)))
    raise SystemExit(0 if result.wasSuccessful() else 1)
