"""Gateway reference-cost accounting, isolated state and intercepted providers.

REVIEW_ROOT can select the 8a33380 baseline. No runtime databases/configuration,
real accounts, generated media or supplier requests are used.
"""
import copy
import json
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from test_cloud_pipeline import CloudTests, m, cp, AsyncImages, GatewayAsyncError, OUTPUT, initial_connections
from gateway_ai import OpenAIImagesEnhance, GatewayError
from text_generation import run_text_job


class CostTrackingTests(CloudTests):
    def setUp(self):
        super().setUp()
        m.settings.update({'prices':{'light':50,'fine':70},'providers':{'worldcodes':{
            'price_light_cny':.08,'price_fine_cny':.32,
            'tiers':{'light':{'price_cny':.07},'fine':{'price_cny':.39}}}},
            'text_generation':{'enabled':True,'model':'fixture-text','base_url':'https://fixture.invalid',
                'api_key':'fixture','price':50,'price_cny':.23}})

    def begin(self,**kwargs):
        jid=self.new(**kwargs);self.step(jid,'prepare')
        with patch.object(AsyncImages,'submit',return_value='imgtask_fixture'):self.step(jid,'submit')
        return jid

    def vendor_done(self,jid,state='completed',url=None):
        result={'status':state,'result':{'data':[{'url':url or 'https://'+cp.MEDIA_HOST+'/images/cost-fixture.png'}]}}
        with patch.object(AsyncImages,'poll',return_value=result):self.step(jid,'generating')
        return m.jobs.get(jid)

    def assert_cost(self,jid,amount):
        job=m.jobs.get(jid)
        self.assertEqual(job.get('cost_cny'),amount)
        self.assertIs(job.get('cost_estimated'),True)
        self.assertEqual(job['provider'],'worldcodes')

    def test_photo_and_template_snapshot_selected_tier_cost_not_photon_price(self):
        m.settings.update({'free_mode':True})
        template={'id':'fixture','name':'模板','prompt':'fixture','engine':'fine','price':999}
        for quality,amount,sale in [('light',.07,50),('fine',.39,70)]:
            for tpl in (None,template):
                jid=self.new(quality=quality,template=tpl);job=m.jobs.get(jid)
                self.assertEqual(job['cloud_request'].get('estimated_cost_cny'),amount)
                self.assertEqual(job['price'],sale)
                self.assertIsNone(job.get('cost_cny'))

    def test_waiting_and_running_tasks_do_not_claim_completed_cost(self):
        jid=self.begin()
        self.assertIsNone(m.jobs.get(jid).get('cost_cny'))
        with patch.object(AsyncImages,'poll',return_value={'status':'running'}):self.step(jid,'generating')
        job=m.jobs.get(jid)
        self.assertIsNone(job.get('cost_cny'));self.assertIsNone(job.get('provider_completed_at'))

    def test_photo_completion_records_cost_before_import_and_admin_exposes_estimate(self):
        jid=self.begin();self.vendor_done(jid)
        self.assert_cost(jid,.07)
        self.assertEqual(m.jobs.get(jid)['cloud_phase'],'import')
        data=self.admin.get('/admin/api/jobs').json()
        item=next(item for item in data['items'] if item['id']==jid)
        self.assertEqual(item['cost_cny'],.07);self.assertIs(item['cost_estimated'],True)
        self.assertEqual(m.users.get_balance('sample_user'),50)

    def test_fine_template_uses_fine_connection_reference_cost(self):
        jid=self.begin(quality='fine',template={'id':'fixture','name':'模板','prompt':'fixture','price':999})
        self.vendor_done(jid,state='succeeded');self.assert_cost(jid,.39)
        self.step(jid,'import');self.step(jid,'finalize')
        self.assertEqual(m.jobs.get(jid)['status'],'succeeded')
        self.assertEqual(m.users.get_balance('sample_user'),30)

    def test_price_change_after_admission_does_not_reprice_existing_job_cost(self):
        jid=self.begin()
        m.settings.update({'providers':{'worldcodes':{'tiers':{'light':{'price_cny':9.99}}}}})
        self.vendor_done(jid);self.assert_cost(jid,.07)

    def test_completed_reference_cost_survives_restart_and_delivery(self):
        jid=self.begin();self.vendor_done(jid)
        db=m.jobs._db_path;m.jobs._conn.close();m.jobs=m.JobStore(2592000,5000,db_path=db)
        self.assert_cost(jid,.07)
        with patch.object(AsyncImages,'submit',side_effect=AssertionError('PAID_REPLAY')):
            self.step(jid,'import');self.step(jid,'finalize')
        self.assert_cost(jid,.07)

    def test_import_failure_refunds_photons_but_retains_generated_reference_expense(self):
        jid=self.begin();self.vendor_done(jid)
        with patch.object(cp.cos,'object_metadata',side_effect=cp.cos.CosError('fixture denial',status=403)):
            self.step(jid,'import')
        self.assertEqual(m.jobs.get(jid)['status'],'failed');self.assert_cost(jid,.07)
        self.assertEqual(m.users.get_balance('sample_user'),100)

    def test_output_rejection_retains_reference_expense_without_user_penalty(self):
        jid=self.begin();self.vendor_done(jid);self.step(jid,'import')
        m.settings.update({'moderation':{'enabled':True}})
        with patch.object(cp.cos,'audit_object',return_value={'result':2,'label':'fixture'}):self.step(jid,'finalize')
        self.assertEqual(m.jobs.get(jid)['status'],'failed');self.assert_cost(jid,.07)
        self.assertEqual(m.users.get_balance('sample_user'),100)

    def test_completed_malformed_or_unsupported_result_keeps_reference_cost(self):
        jid=self.begin();self.vendor_done(jid,url='https://other.invalid/images/fixture.png')
        self.assertEqual(m.jobs.get(jid)['status'],'failed');self.assert_cost(jid,.07)

    def test_supplier_failed_status_does_not_record_reference_cost(self):
        jid=self.begin()
        with patch.object(AsyncImages,'poll',return_value={'status':'failed','error':{'message':'fixture'}}):self.step(jid,'generating')
        self.assertEqual(m.jobs.get(jid)['status'],'failed')
        self.assertIsNone(m.jobs.get(jid).get('cost_cny'))

    def test_uncertain_submit_and_timeout_do_not_claim_confirmed_generation_expense(self):
        jid=self.new();self.step(jid,'prepare')
        with patch.object(AsyncImages,'submit',side_effect=GatewayAsyncError('fixture timeout',uncertain=True)) as submit:
            self.step(jid,'submit');self.cloud.step(jid)
        submit.assert_called_once();self.assertIsNone(m.jobs.get(jid).get('cost_cny'))
        m.jobs.update(jid,deadline=time.time()-1);self.cloud.step(jid)
        self.assertEqual(m.jobs.get(jid)['status'],'failed');self.assertIsNone(m.jobs.get(jid).get('cost_cny'))

    def test_unsubmitted_expired_job_never_gets_provider_cost(self):
        jid=self.new();m.jobs.update(jid,deadline=time.time()-1)
        with patch.object(AsyncImages,'submit') as submit:self.cloud.step(jid)
        submit.assert_not_called();self.assertIsNone(m.jobs.get(jid).get('cost_cny'))

    def test_explicit_zero_cost_is_retained_as_estimate_not_missing(self):
        m.settings.update({'providers':{'worldcodes':{'tiers':{'light':{'price_cny':0}}}}})
        jid=self.begin();self.vendor_done(jid);self.assert_cost(jid,0)
        item=next(j for j in self.admin.get('/admin/api/jobs').json()['items'] if j['id']==jid)
        self.assertIsNotNone(item['cost_cny']);self.assertIs(item['cost_estimated'],True)

    def test_historical_photo_without_snapshot_is_not_backfilled_from_current_price(self):
        jid=self.new();packet=copy.deepcopy(m.jobs.get(jid)['cloud_request']);packet.pop('estimated_cost_cny',None)
        m.jobs.update(jid,cloud_request=packet);self.step(jid,'prepare')
        with patch.object(AsyncImages,'submit',return_value='imgtask_fixture'):self.step(jid,'submit')
        self.vendor_done(jid);self.step(jid,'import');self.step(jid,'finalize')
        self.assertEqual(m.jobs.get(jid)['status'],'succeeded')
        self.assertIsNone(m.jobs.get(jid).get('cost_cny'))

    def test_existing_actual_cost_is_not_overwritten_by_reference_snapshot(self):
        jid=self.begin();m.jobs.update(jid,cost_cny=.123,cost_estimated=False)
        self.vendor_done(jid);self.step(jid,'import');self.step(jid,'finalize')
        job=m.jobs.get(jid);self.assertEqual(job['cost_cny'],.123);self.assertIs(job['cost_estimated'],False)

    def test_cloud_text_uses_independent_text_reference_cost_at_completion(self):
        jid=self.cloud.admit('sample_user',text={'prompt':'fixture','model':'fixture-text','price':50})['job_id']
        self.assertEqual(m.jobs.get(jid)['cloud_request']['estimated_cost_cny'],.23)
        self.step(jid,'prepare')
        with patch.object(AsyncImages,'submit',return_value='imgtask_fixture'):self.step(jid,'submit')
        self.vendor_done(jid);self.assert_cost(jid,.23)
        self.assertEqual(m.users.get_balance('sample_user'),50)

    def local_text(self):
        m.settings.update({'cloud_pipeline':{'enabled':False}})
        with patch.object(m.pool,'submit') as submit:
            reply=self.client.post('/api/text-generation',headers=self.headers,json={'prompt':'fixture','aspect_ratio':'1:1'})
        self.assertEqual(reply.status_code,200,reply.text)
        args=submit.call_args.args
        return reply.json()['job_id'],args[2:]

    def local_photo(self, invalid=False):
        from types import SimpleNamespace
        m.settings.update({'cloud_pipeline':{'enabled':False},'chain':['worldcodes']})
        source=self.d/'input.jpg';source.write_bytes(self.image())
        client=SimpleNamespace(configured=True,last_cost_cny=.07,last_cost_usd=None,last_scale=None,
            enhance=lambda src,dst,**kwargs:Path(dst).write_bytes(self.image()))
        with patch.object(m.pool,'submit'),patch.object(m,'cos_put'),patch.object(m,'cos_head',return_value=True):
            jid=m._register_job('sample_user','light','clear',str(source),'.jpg')['job_id']
        with patch.object(m,'_get_client',return_value=client),patch.object(m,'cos_put'),patch.object(m,'cos_head',return_value=True):
            if invalid:
                with patch.object(m,'_validate_output',side_effect=ValueError('fixture invalid completed result')):
                    m._run_pipeline(jid,'light','clear')
            else:m._run_pipeline(jid,'light','clear')
        return jid

    def test_sync_photo_marks_gateway_reference_cost_as_estimated(self):
        jid=self.local_photo();job=m.jobs.get(jid)
        self.assertEqual(job['status'],'succeeded',job.get('error'))
        self.assert_cost(jid,.07);self.assertEqual(job['cost_source'],'configured_reference')

    def test_sync_photo_completed_output_validation_failure_keeps_reference_cost(self):
        jid=self.local_photo(invalid=True);job=m.jobs.get(jid)
        self.assertEqual(job['status'],'failed');self.assert_cost(jid,.07)
        self.assertEqual(m.users.get_balance('sample_user'),100)

    def text_generate(self,client,path,*args):Path(path).write_bytes(self.image((90,90)))

    def test_local_text_success_uses_queued_provider_reference_price_snapshot(self):
        jid,args=self.local_text();m.settings.update({'text_generation':{'price_cny':9.99}})
        with patch.object(OpenAIImagesEnhance,'generate',new=lambda client,path,*args:self.text_generate(client,path,*args)),patch.object(m,'cos_put'),patch.object(m,'cos_head',return_value=True):
            run_text_job(m,*args)
        self.assertEqual(m.jobs.get(jid)['status'],'succeeded',m.jobs.get(jid).get('error'))
        self.assert_cost(jid,.23);self.assertEqual(m.users.get_balance('sample_user'),50)

    def test_local_text_post_generation_failure_keeps_estimated_expense(self):
        jid,args=self.local_text()
        with patch.object(OpenAIImagesEnhance,'generate',new=lambda client,path,*args:self.text_generate(client,path,*args)),patch.object(m,'_finalize',side_effect=ValueError('fixture delivery failure')):
            run_text_job(m,*args)
        self.assertEqual(m.jobs.get(jid)['status'],'failed');self.assert_cost(jid,.23)
        self.assertEqual(m.users.get_balance('sample_user'),100)

    def test_local_text_uncertain_or_rejected_generation_does_not_get_cost(self):
        jid,args=self.local_text()
        with patch.object(OpenAIImagesEnhance,'generate',side_effect=GatewayError('fixture unknown',uncertain=True)) as generate:
            run_text_job(m,*args)
        generate.assert_called_once();self.assertEqual(m.jobs.get(jid)['status'],'failed')
        self.assertIsNone(m.jobs.get(jid).get('cost_cny'))


if __name__=='__main__':
    names=[name for name in CostTrackingTests.__dict__ if name.startswith('test_')]
    result=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(CostTrackingTests(name) for name in names))
    failed={test._testMethodName for test,_ in result.failures+result.errors}
    (OUTPUT/'gateway_cost_tracking_results.json').write_text(json.dumps({'cases':[{'case':name,'passed':name not in failed} for name in names]},indent=2),encoding='utf8')
    for connection in initial_connections:
        try:connection.close()
        except Exception:pass
    print('GATEWAY_COST_TRACKING_SUMMARY total=%d passed=%d failed=%d'%(result.testsRun,result.testsRun-len(failed),len(failed)))
    raise SystemExit(0 if result.wasSuccessful() else 1)
