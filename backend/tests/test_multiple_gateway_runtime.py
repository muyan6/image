"""Offline real admission/worker transitions for named sync/async gateways."""
import base64
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import requests
from test_cloud_pipeline import CloudTests,m,cp,AsyncImages,GatewayAsyncError,OUTPUT,initial_connections
from gateway_ai import GatewayError,OpenAIImagesEnhance
from gateway_runtime import gateway_snapshots
from gateway_registry import gateway_defaults

A='gw_'+'a'*32
B='gw_'+'b'*32

class MultipleGatewayRuntimeTests(CloudTests):
    def registry(self,modes=('async','async')):
        providers={key:None for key in m.settings.snapshot()['providers']}
        for index,(gid,mode) in enumerate(zip((A,B),modes)):
            providers[gid]={**gateway_defaults(gid),'name':['第一网关','第二网关'][index],
                'enabled':True,'base_url':['https://a.invalid','https://b.invalid'][index],
                'api_key':['fixture-key-a','fixture-key-b'][index],
                'model_light':'fixture-'+gid[-1],'model_fine':'fine-'+gid[-1],
                'endpoint':'/different/'+gid[-1]+'/images','request_mode':mode,
                'price_light_cny':[.04,.12][index],'price_fine_cny':.3}
        m.settings.update({'providers':providers,'chain':[A,B]})

    def frozen_job(self):
        jid=self.new();self.step(jid,'prepare');return m.jobs.get(jid)

    def local_job(self):
        source=self.d/'owned-source.jpg';source.write_bytes(self.image())
        with patch.object(m.pool,'submit'),patch.object(m,'cos_put',return_value=None):
            response=m._register_job('sample_user','light','',str(source),'.jpg',expected_price=40)
        return response['job_id']

    def response(self,status=200):
        image=self.image()
        class Response:
            status_code=status;headers={'content-type':'application/json'};text='fixture refusal'
            def json(self):return {'data':[{'b64_json':base64.b64encode(image).decode()}]}
        return Response()

    def sync_io(self,stack,post):
        stack.enter_context(patch.object(requests.Session,'post',side_effect=post))
        stack.enter_context(patch.object(m,'cos_put',return_value=None))
        stack.enter_context(patch.object(m,'cos_head',return_value=True))
        stack.enter_context(patch.object(m,'cos_get',return_value=self.image()))
        stack.enter_context(patch.object(m,'_moderate_or_reject',return_value=None))

    def test_multiple_async_priority_reject_then_next_same_reservation_and_no_raw_keys(self):
        self.registry();job=self.frozen_job();calls=[]
        def submit(client,*args):
            calls.append(client.conf['gateway_id'])
            if client.conf['gateway_id']==A:raise GatewayAsyncError('explicit 401',status=401)
            return 'imgtask_second'
        with patch.object(AsyncImages,'submit',new=submit):
            self.step(job['id'],'submit');intermediate=m.jobs.get(job['id'])
            self.assertEqual(intermediate['cloud_phase'],'submit');self.assertIsNone(intermediate['submitted_at'])
            self.step(job['id'],'submit')
        self.assertEqual(calls,[A,B]);self.assertEqual(m.users.get_balance('sample_user'),60)
        self.assertEqual(m.users._conn.execute('SELECT COUNT(*) FROM job_charges WHERE job_id=?',(job['id'],)).fetchone()[0],1)
        packet=m.jobs.get(job['id'])['cloud_request'];raw=json.dumps(packet)
        for value in ('fixture-key-a','fixture-key-b','api_key'):self.assertNotIn(value,raw)
        self.completed(job['id']);finished=m.jobs.get(job['id'])
        self.assertEqual(finished['provider'],B);self.assertEqual(finished['provider_name'],'第二网关');self.assertEqual(finished['cost_cny'],.12)

    def test_multiple_queued_reorder_stays_on_frozen_id_and_pre_submit_changes_fail(self):
        self.registry();job=self.frozen_job();m.settings.update({'chain':[B,A]});calls=[]
        def submit(client,*args):calls.append(client.conf['gateway_id']);return 'imgtask_original'
        with patch.object(AsyncImages,'submit',new=submit):self.step(job['id'],'submit')
        self.assertEqual(calls,[A])
        other=self.frozen_job();m.settings.update({'providers':{B:{'base_url':'https://changed.invalid'}}})
        with patch.object(AsyncImages,'submit',side_effect=AssertionError('NO_CHANGED_HOST_POST')):
            self.step(other['id'],'submit')
        self.assertEqual(m.jobs.get(other['id'])['status'],'failed')

    def test_multiple_accepted_disable_rename_model_change_reorder_poll_original_snapshot(self):
        self.registry();job=self.frozen_job()
        with patch.object(AsyncImages,'submit',return_value='imgtask_original'):self.step(job['id'],'submit')
        m.settings.update({'chain':[B,A],'providers':{A:{'enabled':False,'name':'新名字','model_light':'new-model','request_mode':'sync'}}})
        seen=[]
        def poll(client,task):seen.append((client.conf['gateway_id'],client.base,client.conf['gateway_name']));return {'status':'processing'}
        with patch.object(AsyncImages,'poll',new=poll):self.step(job['id'],'generating')
        self.assertEqual(seen,[(A,'https://a.invalid','第一网关')]);self.assertEqual(m.users.get_balance('sample_user'),60)

    def test_multiple_accepted_deleted_or_rotated_id_never_borrows_other_key(self):
        self.registry();job=self.frozen_job()
        with patch.object(AsyncImages,'submit',return_value='imgtask_original'):self.step(job['id'],'submit')
        m.settings.update({'providers':{A:None},'chain':[B]})
        with patch.object(AsyncImages,'poll',side_effect=AssertionError('NO_OTHER_GATEWAY_KEY')):self.step(job['id'],'generating')
        self.assertEqual(m.jobs.get(job['id'])['status'],'failed');self.assertEqual(m.users.get_balance('sample_user'),100)

    def test_multiple_ambiguous_post_and_server_failure_do_not_fallback_or_resubmit(self):
        self.registry();job=self.frozen_job();calls=[]
        def submit(client,*args):calls.append(client.conf['gateway_id']);raise GatewayAsyncError('unknown',uncertain=True,status=503)
        with patch.object(AsyncImages,'submit',new=submit):
            self.step(job['id'],'submit');self.cloud.step(job['id']);self.cloud.step(job['id'])
        self.assertEqual(calls,[A]);self.assertEqual(m.jobs.get(job['id'])['cloud_phase'],'unknown')
        self.assertIsNone(m.jobs.get(job['id']).get('cost_cny'))

    def test_multiple_accepted_vendor_failure_never_calls_second_gateway(self):
        self.registry();job=self.frozen_job()
        with patch.object(AsyncImages,'submit',return_value='imgtask_accepted'):self.step(job['id'],'submit')
        with patch.object(AsyncImages,'submit',side_effect=AssertionError('NO_SECOND_PAID_POST')),patch.object(AsyncImages,'poll',return_value={'status':'failed','error':{'message':'vendor fail'}}):self.step(job['id'],'generating')
        self.assertEqual(m.jobs.get(job['id'])['status'],'failed');self.assertEqual(m.users.get_balance('sample_user'),100)

    def test_multiple_async_endpoint_exact_override_and_no_double_async_suffix(self):
        self.registry();conf=m.settings.gateway_for('light',A);paths=[]
        for endpoint,override,expected in [('/images/custom','/jobs/post','/jobs/post'),('/images/custom/async','','/images/custom/async'),('/images/custom','','/images/custom/async')]:
            client=AsyncImages({**conf,'endpoint':endpoint,'async_endpoint':override})
            with patch.object(client,'request',side_effect=lambda method,path,body:(paths.append(path) or (202,{'task_id':'imgtask_path'}))):client.submit('fixture','prompt')
            self.assertEqual(paths[-1],expected)

    def test_multiple_sync_uses_actual_custom_post_paths_in_priority_order_and_no_local_model(self):
        from contextlib import ExitStack
        self.registry(('sync','sync'));jid=self.local_job();calls=[]
        def post(url,**kwargs):calls.append(url);return self.response(401 if url.startswith('https://a.invalid') else 200)
        with ExitStack() as stack:
            self.sync_io(stack,post);stack.enter_context(patch.object(m.engine,'process',side_effect=AssertionError('NO_LOCAL_GENERATION')))
            m._run_pipeline(jid,'light','')
        self.assertEqual(calls,['https://a.invalid/different/a/images','https://b.invalid/different/b/images'])
        job=m.jobs.get(jid);self.assertEqual(job['status'],'succeeded',job.get('error'));self.assertEqual(job['provider'],B);self.assertEqual(job['cost_cny'],.12)

    def test_multiple_sync_unknown_output_or_network_never_calls_next_gateway(self):
        from contextlib import ExitStack
        self.registry(('sync','sync'));jid=self.local_job();calls=[]
        def post(url,**kwargs):calls.append(url);raise requests.Timeout('fixture timeout')
        with ExitStack() as stack:self.sync_io(stack,post);m._run_pipeline(jid,'light','')
        self.assertEqual(len(calls),1);self.assertEqual(m.jobs.get(jid)['status'],'failed');self.assertEqual(m.users.get_balance('sample_user'),100)

    def test_multiple_cloud_rejected_async_to_sync_handoff_keeps_job_charge_once(self):
        from contextlib import ExitStack
        self.registry(('async','sync'));job=self.frozen_job();queued=[]
        with patch.object(AsyncImages,'submit',side_effect=GatewayAsyncError('explicit 403',status=403)):
            self.step(job['id'],'submit')
        with patch.object(m.pool,'submit',side_effect=lambda fn,*args:queued.append((fn,args))):self.cloud.run_ready_steps(job['id'])
        current=m.jobs.get(job['id'])
        self.assertFalse(current['cloud_pipeline']);self.assertEqual(current['cloud_phase'],'sync_queued')
        self.assertEqual(current['status'],'processing');self.assertEqual(len(queued),1)
        with ExitStack() as stack:self.sync_io(stack,lambda url,**kwargs:self.response());queued[0][0](*queued[0][1])
        done=m.jobs.get(job['id']);self.assertEqual(done['status'],'succeeded',done.get('error'));self.assertEqual(done['provider'],B)
        self.assertEqual(m.users.get_balance('sample_user'),60);self.assertEqual(m.users._conn.execute('SELECT COUNT(*) FROM job_charges WHERE job_id=?',(job['id'],)).fetchone()[0],1)

    def test_multiple_sync_rejected_to_async_handoff_keeps_job_charge_once(self):
        from contextlib import ExitStack
        self.registry(('sync','async'));jid=self.local_job();calls=[]
        with ExitStack() as stack:
            self.sync_io(stack,lambda url,**kwargs:(calls.append(url) or self.response(401)))
            stack.enter_context(patch.object(self.cloud,'origin_ready',return_value=True));m._run_pipeline(jid,'light','')
        job=m.jobs.get(jid);self.assertTrue(job['cloud_pipeline']);self.assertEqual(job['cloud_phase'],'submit');self.assertEqual(job['cloud_request']['gateway_id'],B)
        with patch.object(AsyncImages,'submit',return_value='imgtask_handoff'):self.step(jid,'submit')
        self.completed(jid);self.assertEqual(m.jobs.get(jid)['status'],'succeeded',m.jobs.get(jid).get('error'));self.assertEqual(m.jobs.get(jid)['provider'],B)
        self.assertEqual(m.users.get_balance('sample_user'),60);self.assertEqual(m.users._conn.execute('SELECT COUNT(*) FROM job_charges WHERE job_id=?',(jid,)).fetchone()[0],1)

    def test_multiple_sync_first_uses_bounded_worker_and_queue_failure_refunds(self):
        self.registry(('sync','async'));job=self.frozen_job();queued=[]
        with patch.object(m.pool,'submit',side_effect=lambda fn,*args:queued.append((fn,args))):self.step(job['id'],'submit')
        self.assertEqual(len(queued),1);self.assertFalse(m.jobs.get(job['id'])['cloud_pipeline'])
        other=self.new();self.step(other,'prepare')
        with patch.object(m.pool,'submit',side_effect=m.QueueFull('fixture queue full')):self.step(other,'submit')
        self.assertEqual(m.jobs.get(other)['status'],'failed');self.assertEqual(m.users.get_balance('sample_user'),60)

    def test_multiple_all_disabled_reject_before_reserving_and_health_has_no_retired_modes(self):
        self.registry();m.settings.update({'providers':{A:{'enabled':False},B:{'enabled':False}}})
        with self.assertRaises(m.HTTPException) as result:self.new()
        self.assertEqual(result.exception.status_code,503);self.assertEqual(m.users.get_balance('sample_user'),100)
        with patch.object(m.settings,'cos_ready',return_value=False):health=m.health()
        for value in ('fal','baidu','local'):self.assertNotIn(value,health)
        self.assertFalse(health['configured']);self.assertEqual(len(health['gateways']),2)
        self.assertNotIn('fixture-key',json.dumps(health))

    def test_multiple_text_connection_does_not_borrow_photo_priority_and_records_own_id(self):
        self.registry();m.settings.update({'text_generation':{'enabled':True,'base_url':'https://text.invalid','api_key':'fixture-text-key','model':'text-model','endpoint':'/separate/generate','price_cny':.23}})
        text=m.settings.text_generation();jid=self.cloud.admit('sample_user',text={**text,'prompt':'水彩','size':'1024x1024'})['job_id'];self.step(jid,'prepare');seen=[]
        def submit(client,*args):seen.append((client.conf['gateway_id'],client.base,args[-1]));return 'imgtask_text'
        with patch.object(AsyncImages,'submit',new=submit):self.step(jid,'submit')
        self.assertEqual(seen,[('text_generation','https://text.invalid','/separate/generate')]);self.completed(jid)
        self.assertEqual(m.jobs.get(jid)['provider'],'text_generation');self.assertEqual(m.jobs.get(jid)['cost_cny'],.23)

    def test_multiple_client_cache_prunes_deleted_ids_without_closing_active_client(self):
        self.registry();m._clients.clear();client=m._get_client(A,'light');self.assertIsNotNone(client)
        m.settings.update({'providers':{A:None},'chain':[B]});m._get_client(B,'light')
        self.assertTrue(all(key[0]!=A for key in m._clients));self.assertTrue(client.configured)

    def test_multiple_non_sql_cost_hook_failure_rolls_back_sql_and_memory_atomically(self):
        self.registry();job=self.frozen_job();before=copy.deepcopy(m.jobs.get(job['id']))
        with patch.object(m,'record_cost',side_effect=RuntimeError('fixture cost hook fail')):
            with self.assertRaises(RuntimeError):m.jobs.update(job['id'],status='succeeded',cost_cny=.04)
        self.assertEqual(m.jobs.get(job['id']),before)
        self.assertEqual(m.jobs._conn.execute('SELECT status,cost_cny FROM jobs WHERE id=?',(job['id'],)).fetchone(),('processing',None))
        self.assertFalse(m.jobs._conn.in_transaction);self.assertEqual(m.users.get_balance('sample_user'),60)
        self.assertEqual(m.jobs._conn.execute('SELECT COUNT(*) FROM gateway_cost_ledger WHERE job_id=?',(job['id'],)).fetchone()[0],0)

    def delete_owned(self,jid):
        response=self.client.delete('/api/my/jobs/'+jid,headers=self.headers)
        self.assertEqual(response.status_code,200,response.text)
        return response.json()

    def test_multiple_delete_during_normalization_stops_all_paid_posts_and_refunds(self):
        from contextlib import ExitStack
        self.registry(('sync','sync'));jid=self.local_job();normalize=m._normalize_long_side
        def normalize_and_delete(*args,**kwargs):
            normalize(*args,**kwargs);self.delete_owned(jid)
        with ExitStack() as stack:
            self.sync_io(stack,lambda *args,**kwargs:self.fail('POST_AFTER_UNSUBMITTED_DELETE'))
            stack.enter_context(patch.object(m,'_normalize_long_side',side_effect=normalize_and_delete))
            m._run_pipeline(jid,'light','')
        job=m.jobs.get(jid);self.assertTrue(job['deleted_at']);self.assertFalse(job['cancel_without_refund'])
        self.assertEqual(m.users.get_balance('sample_user'),100)

    def test_multiple_delete_inside_first_rejection_does_not_post_next_gateway_and_refunds(self):
        from contextlib import ExitStack
        self.registry(('sync','sync'));jid=self.local_job();calls=[]
        def post(url,**kwargs):
            calls.append(url);self.assertTrue(m.jobs.get(jid)['submitted_at'])
            self.assertEqual(self.delete_owned(jid)['balance'],60)
            return self.response(401)
        with ExitStack() as stack:self.sync_io(stack,post);m._run_pipeline(jid,'light','')
        self.assertEqual(calls,['https://a.invalid/different/a/images'])
        job=m.jobs.get(jid);self.assertIsNone(job['submitted_at']);self.assertFalse(job['cancel_without_refund'])
        self.assertEqual(m.users.get_balance('sample_user'),100)
        self.assertIsNone(job.get('cost_cny'))

    def test_multiple_same_gateway_url_multipart_json_and_size_retries_recheck_deleted(self):
        from contextlib import ExitStack
        for status,cos_enabled in [(404,True),(422,False),(400,False)]:
            with self.subTest(status=status,cos_enabled=cos_enabled):
                # Each iteration has an independent frozen job and original debit.
                self.registry(('sync','sync'));m.users._conn.execute("UPDATE users SET balance=100 WHERE openid='sample_user'");m.users._conn.commit()
                jid=self.local_job();calls=[]
                def post(url,**kwargs):calls.append(url);self.delete_owned(jid);return self.response(status)
                with ExitStack() as stack:
                    self.sync_io(stack,post)
                    if not cos_enabled:stack.enter_context(patch.object(m.settings,'cos_ready',return_value=False))
                    m._run_pipeline(jid,'light','',template={'gateway_size':'1024x1024'})
                self.assertEqual(len(calls),1);self.assertEqual(m.users.get_balance('sample_user'),100)
                self.assertFalse(m.jobs.get(jid)['cancel_without_refund'])

    def test_multiple_successful_submitted_response_after_delete_keeps_debit_and_known_cost(self):
        from contextlib import ExitStack
        self.registry(('sync','sync'));jid=self.local_job();calls=[]
        def post(url,**kwargs):
            calls.append(url);self.assertEqual(self.delete_owned(jid)['balance'],60)
            return self.response()
        with ExitStack() as stack:self.sync_io(stack,post);m._run_pipeline(jid,'light','')
        job=m.jobs.get(jid);self.assertEqual(len(calls),1);self.assertTrue(job['deleted_at'])
        self.assertEqual(job['status'],'failed');self.assertTrue(job['cancel_without_refund'])
        self.assertEqual(m.users.get_balance('sample_user'),60);self.assertEqual(job['cost_cny'],.04)
        self.assertEqual(m.users._conn.execute('SELECT state,refunded FROM job_charges WHERE job_id=?',(jid,)).fetchone(),('cancelled_charged',0))

    def test_multiple_blank_endpoint_and_mode_path_frozen_before_post(self):
        from gateway_runtime import resolve_gateway
        self.registry();m.settings.update({'providers':{A:{'endpoint':''}}})
        job=self.frozen_job();packet=job['cloud_request']
        self.assertEqual(packet['endpoint'],'/v1/images/edits')
        self.assertEqual(resolve_gateway(m.settings,'light',packet)['endpoint'],'/v1/images/edits')
        for changed in [{'request_mode':'sync'},{'async_endpoint':'/changed/post'}]:
            with self.subTest(changed=changed):
                m.settings.update({'providers':{A:{'request_mode':'async','async_endpoint':''}}})
                candidate=self.frozen_job();m.settings.update({'providers':{A:changed}})
                with patch.object(AsyncImages,'submit',side_effect=AssertionError('NO_CHANGED_POST_MODE')):self.step(candidate['id'],'submit')
                self.assertEqual(m.jobs.get(candidate['id'])['status'],'failed')

    def test_multiple_sync_restart_and_reentry_never_resubmits_marked_post(self):
        self.registry(('sync','sync'));jid=self.local_job()
        self.assertTrue(m.jobs.begin_sync_submission(jid,A,'fixture-attempt'))
        with patch.object(requests.Session,'post',side_effect=AssertionError('NO_DUPLICATE_POST')):m._run_pipeline(jid,'light','')
        self.assertEqual(m.jobs.get(jid)['status'],'processing')
        db=m.jobs._db_path;m.jobs._conn.close();m.jobs=m.JobStore(2592000,5000,db_path=db)
        self.assertEqual(m.jobs.get(jid)['status'],'failed')
        with patch.object(requests.Session,'post',side_effect=AssertionError('NO_RESTART_POST')):m._run_pipeline(jid,'light','')
        self.assertEqual(m.users.get_balance('sample_user'),100)

    def test_multiple_refusal_clears_paid_marker_before_compatibility_encoding_failure(self):
        from contextlib import ExitStack
        self.registry(('sync','sync'));jid=self.local_job();calls=[]
        client=m._get_client(A,'light')
        def compress(*args):
            self.assertIsNone(m.jobs.get(jid)['submitted_at'])
            self.assertEqual(self.delete_owned(jid)['balance'],100)
            raise GatewayError('fixture local encoding failed',code='BAD_IMAGE')
        with ExitStack() as stack:
            self.sync_io(stack,lambda url,**kwargs:(calls.append(url) or self.response(400)))
            stack.enter_context(patch.object(client,'_maybe_compress',side_effect=compress))
            m._run_pipeline(jid,'light','')
        self.assertEqual(len(calls),1);self.assertEqual(m.users.get_balance('sample_user'),100)
        self.assertFalse(m.jobs.get(jid)['cancel_without_refund'])

    def test_multiple_async_explicit_refusal_after_delete_refunds_without_handoff(self):
        self.registry(('async','sync'));job=self.frozen_job();calls=[]
        def submit(*args,**kwargs):
            calls.append('post');self.assertEqual(self.delete_owned(job['id'])['balance'],60)
            raise GatewayAsyncError('known refusal',status=403)
        with patch.object(AsyncImages,'submit',side_effect=submit),patch.object(m.pool,'submit',side_effect=AssertionError('NO_DELETED_HANDOFF')):
            self.cloud.run_ready_steps(job['id'])
        cancelled=m.jobs.get(job['id']);self.assertEqual(calls,['post'])
        self.assertFalse(cancelled['cancel_without_refund']);self.assertIsNone(cancelled['submitted_at'])
        self.assertEqual(m.users.get_balance('sample_user'),100)

    def test_multiple_async_to_sync_preserves_admitted_custom_prompt_and_client_has_no_stale_guard(self):
        from contextlib import ExitStack
        self.registry(('async','sync'));jid=self.new(custom_prompt='只增强猫咪，不改变背景')
        self.step(jid,'prepare');queued=[];calls=[]
        with patch.object(AsyncImages,'submit',side_effect=GatewayAsyncError('known refusal',status=401)):
            self.step(jid,'submit')
        m.settings.update({'tiers':{'light':{'prompt':'配置已改后的其它提示'}}})
        with patch.object(m.pool,'submit',side_effect=lambda fn,*args:queued.append((fn,args))):self.cloud.run_ready_steps(jid)
        def post(url,**kwargs):
            calls.append(kwargs['json']['prompt']);return self.response()
        with ExitStack() as stack:self.sync_io(stack,post);queued[0][0](*queued[0][1])
        self.assertEqual(m.jobs.get(jid)['status'],'succeeded');self.assertIn('只增强猫咪，不改变背景',calls[0])
        self.assertNotIn('配置已改后的其它提示',calls[0])
        # Same cached client can serve a direct/offline call without retaining
        # the previous job's guard or cancellation/rejection closure.
        client=m._get_client(B,'light');source=self.d/'direct.jpg';out=self.d/'out.jpg';source.write_bytes(self.image())
        with patch.object(requests.Session,'post',return_value=self.response()):client.enhance(str(source),str(out),quality='light')
        self.assertTrue(out.is_file());self.assertEqual(m.users.get_balance('sample_user'),60)

    def text_config(self):
        m.settings.update({'text_generation':{'enabled':True,'base_url':'https://text.invalid',
            'api_key':'fixture-text-key','model':'configured-text','endpoint':'/configured/generations','price':40,'price_cny':.23}})

    def test_multiple_trusted_internal_text_model_and_post_path_execute_not_live_photo_defaults(self):
        self.registry();self.text_config();sent=[]
        jid=self.cloud.admit('sample_user',text={'prompt':'水彩','model':'trusted-text',
            'endpoint':'/internal/custom-generations','size':'1024x1024','price':40})['job_id']
        packet=m.jobs.get(jid)['cloud_request']
        self.assertEqual((packet['config_model'],packet['config_endpoint']),('configured-text','/configured/generations'))
        def request(client,method,path,body=None):
            sent.append((client.base,method,path,body));return 202,{'task_id':'imgtask_text_custom'}
        with patch.object(AsyncImages,'request',new=request):self.step(jid,'submit')
        self.assertEqual(sent[0][:3],('https://text.invalid','POST','/internal/custom-generations/async'))
        self.assertEqual(sent[0][3]['model'],'trusted-text')
        self.assertEqual(m.jobs.get(jid)['cloud_phase'],'generating')
        self.assertNotIn('fixture-text-key',json.dumps(packet));self.assertEqual(m.users.get_balance('sample_user'),60)

    def test_multiple_text_connection_changes_between_binding_and_admission_reject_before_reserve(self):
        from gateway_runtime import text_configuration_snapshot
        self.registry();self.text_config();conf=m.settings.text_generation()
        binding=text_configuration_snapshot(m.settings,conf)
        def ready(*args,**kwargs):
            m.settings.update({'text_generation':{'model':'new-text','endpoint':'/new/generations'}});return True
        with patch.object(self.cloud,'ready',side_effect=ready):
            with self.assertRaises(m.HTTPException) as result:
                self.cloud.admit('sample_user',text={**conf,'prompt':'水彩','size':'1024x1024','connection_snapshot':binding})
        self.assertEqual(result.exception.status_code,503)
        self.assertEqual(m.users.get_balance('sample_user'),100)
        self.assertEqual(m.users._conn.execute('SELECT COUNT(*) FROM job_charges').fetchone()[0],0)
        self.assertEqual(m.jobs._conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0],0)

    def test_multiple_queued_text_internal_override_still_checks_original_connection_model_path(self):
        self.registry();self.text_config()
        jid=self.cloud.admit('sample_user',text={'prompt':'水彩','model':'trusted-text',
            'endpoint':'/internal/custom-generations','price':40})['job_id']
        m.settings.update({'text_generation':{'endpoint':'/changed/generations'}})
        with patch.object(AsyncImages,'request',side_effect=AssertionError('NO_CHANGED_TEXT_POST')):self.step(jid,'submit')
        self.assertEqual(m.jobs.get(jid)['status'],'failed');self.assertEqual(m.users.get_balance('sample_user'),100)

    def test_multiple_public_text_route_binds_effective_conf_before_admission_window(self):
        self.registry();self.text_config();calls=[]
        def ready(*args,**kwargs):
            calls.append('ready')
            if len(calls)==2:m.settings.update({'text_generation':{'model':'new-text','endpoint':'/new/generations','price':80}})
            return True
        with patch.object(self.cloud,'ready',side_effect=ready),patch.object(m,'_moderate_text_or_reject',return_value=None):
            response=self.client.post('/api/text-generation',headers=self.headers,
                json={'prompt':'水彩','aspect_ratio':'1:1','expected_price':40})
        self.assertEqual(response.status_code,503,response.text);self.assertEqual(len(calls),2)
        self.assertEqual(m.users.get_balance('sample_user'),100)
        self.assertEqual(m.users._conn.execute('SELECT COUNT(*) FROM job_charges').fetchone()[0],0)

if __name__=='__main__':
    names=[name for name in MultipleGatewayRuntimeTests.__dict__ if name.startswith('test_multiple_')]
    result=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(MultipleGatewayRuntimeTests(name) for name in names))
    failed={test._testMethodName for test,_ in result.failures+result.errors}
    (OUTPUT/'multiple_gateway_runtime_results.json').write_text(json.dumps({'cases':[{'case':name,'passed':name not in failed} for name in names],
        'summary':{'total':result.testsRun,'passed':result.testsRun-len(failed),'failed':len(failed)}},ensure_ascii=False,indent=2),encoding='utf-8')
    for conn in initial_connections:
        try:conn.close()
        except Exception:pass
    print('MULTIPLE_GATEWAY_RUNTIME_SUMMARY total=%d passed=%d failed=%d'%(result.testsRun,result.testsRun-len(failed),len(failed)))
    raise SystemExit(0 if result.wasSuccessful() else 1)
