"""Remaining audit boundaries and commerce display contracts; isolated/offline."""
import json,time,re,unittest
from unittest.mock import patch
from test_cloud_pipeline import CloudTests,m,cp,initial_connections,OUTPUT
from test_cloud_audit import AuditTests
from gateway_async import AsyncImages,GatewayAsyncError
from cloud_layout import text_rule
from credit_packages import public_packages,default_commerce,next_offer_change,resolve_offer

class PendingTests(CloudTests):
 def test_final_import_probe_accepts_ready_object_without_new_trigger(self):
  jid=self.new();m.jobs.update(jid,cloud_phase='import',vendor_result_key='images/ready.png',cloud_import_started_at=time.time()-121)
  self.cloud.step(jid)
  self.assertEqual(m.jobs.get(jid)['cloud_phase'],'finalize');cp.cos.trigger_mirror.assert_not_called()
  self.assertEqual(m.users.get_balance('sample_user'),60)

 def test_prepare_retries_transient_error_then_resumes(self):
  jid=self.new()
  with patch.object(cp.cos,'object_metadata',side_effect=cp.cos.CosError('temporary',status=503)):self.step(jid,'prepare')
  j=m.jobs.get(jid);self.assertEqual(j['status'],'processing');self.assertEqual(j['phase_retry_counts']['prepare'],1)
  self.assertGreater(j['cloud_next_at'],time.time());self.cloud.step(jid)
  self.assertEqual(m.jobs.get(jid)['cloud_phase'],'submit')

 def test_finalize_retries_without_resubmitting_paid_generation(self):
  jid=self.generating();m.jobs.update(jid,cloud_phase='finalize',vendor_result_key='images/done.png',cloud_import_info={'width':1024,'height':1536})
  with patch.object(cp.cos,'process_image',side_effect=cp.cos.CosError('temporary',status=503)):self.cloud.step(jid)
  self.assertEqual(m.jobs.get(jid)['status'],'processing');self.assertEqual(m.users.get_balance('sample_user'),60)
  with patch.object(AsyncImages,'submit',side_effect=AssertionError('NO_PAID_RESUBMIT')):self.cloud.step(jid)
  self.assertEqual(m.jobs.get(jid)['status'],'succeeded');self.assertEqual(m.users.get_balance('sample_user'),60)

 def test_cos_retries_are_bounded_and_refund_once(self):
  jid=self.new()
  with patch.object(cp.cos,'object_metadata',side_effect=cp.cos.CosError('temporary',code='NETWORK')):
   for _ in range(5):self.step(jid,'prepare') if m.jobs.get(jid)['status']=='processing' else self.cloud.step(jid)
  self.assertEqual(m.jobs.get(jid)['status'],'failed');self.assertEqual(m.users.get_balance('sample_user'),100)
  self.assertEqual(m.jobs.get(jid)['phase_retry_counts']['prepare'],3)

 def test_permanent_poll_error_is_terminal_transient_is_not(self):
  for status,expected in [(401,'failed'),(403,'failed'),(429,'processing'),(503,'processing')]:
   jid=self.generating()
   with patch.object(AsyncImages,'poll',side_effect=GatewayAsyncError('HTTP '+str(status),status=status)):self.cloud.step(jid)
   self.assertEqual(m.jobs.get(jid)['status'],expected)
   if expected=='processing':self.cloud.fail(m.jobs.get(jid),'fixture cleanup')

 def test_non_json_401_keeps_permanent_http_status(self):
  from test_cloud_origin import response
  r=response(401,b'<html>unauthorized</html>')
  with patch('gateway_async.http_transport.request',return_value=r):
   with self.assertRaises(GatewayAsyncError) as caught:AsyncImages({'base_url':'https://fixture.invalid','api_key':'fixture'}).poll('imgtask_fixture')
  self.assertEqual(caught.exception.status,401);self.assertFalse(caught.exception.retryable)

 def test_owner_pagination_reaches_oldest_and_does_not_leak(self):
  for i in range(101):m.jobs.create(f'{i:012x}',openid='sample_user',created_at=time.time()+i)
  m.jobs.create('other0000000',openid='other_user')
  a=self.client.get('/api/my/jobs?limit=100',headers=self.headers).json()
  b=self.client.get('/api/my/jobs?limit=100&offset=100',headers=self.headers).json()
  self.assertTrue(a['has_more']);self.assertFalse(b['has_more']);self.assertEqual(len(b['jobs']),1)
  self.assertEqual(len({x['id'] for x in a['jobs']+b['jobs']}),101)

 def test_price_quote_change_rejects_before_debit_or_task_creation(self):
  with self.assertRaises(m.HTTPException) as caught:self.cloud.admit('sample_user',source=self.source,expected_price=99)
  self.assertEqual(caught.exception.status_code,409);self.assertEqual(m.users.get_balance('sample_user'),100)
  self.assertEqual(m.jobs.list_recent()[1],0)

 def test_layout_uses_cumulative_heights_for_all_presets(self):
  for layout in ('postcard_bottom','poster_center','stamp_corner'):
   tpl={'layout':layout,'text_fields':[{'key':r,'role':r} for r in ('title','subtitle','date')]}
   rule=text_rule(1024,1536,tpl,{'title':'长'*70,'subtitle':'地点','date':'2026.10.01'})
   positions=[(int(re.search(r'/dy/(\d+)',s)[1]),int(re.search(r'/fontsize/(\d+)',s)[1])) for s in rule.split('|')]
   for (y,size),(next_y,_) in zip(positions,positions[1:]):self.assertGreaterEqual(next_y,y+size+8)

 def test_commerce_metadata_before_during_after_sale(self):
  c=default_commerce()['packages'];c[0]['promotion'].update(enabled=True,starts_at=100,ends_at=200,amount_fen=480,points=800,product_id='holiday')
  before=public_packages(catalog=c,now=99)[0];active=public_packages(catalog=c,now=100)[0];after=public_packages(catalog=c,now=200)[0]
  self.assertEqual(before['regular_price_text'],'');self.assertEqual(active['regular_price_text'],'¥6');self.assertEqual(active['promotion_ends_at'],200)
  self.assertEqual(after['price_text'],'¥6');self.assertFalse(after['promotion_active'])
  self.assertEqual(next_offer_change(c,99),100);self.assertEqual(next_offer_change(c,100),200);self.assertEqual(next_offer_change(c,200),0)
  with self.assertRaises(ValueError):resolve_offer(c,active['id'],now=200)

 def test_same_price_bonus_has_no_fictitious_strike_price(self):
  c=default_commerce()['packages'];c[0]['promotion'].update(enabled=True,starts_at=100,ends_at=200,points=1200)
  p=public_packages(catalog=c,now=150)[0];self.assertTrue(p['promotion_active']);self.assertEqual(p['regular_price_text'],'')

 def test_packages_api_exposes_server_clock_and_schedule_not_keys(self):
  d=self.client.get('/api/credits/packages').json();self.assertLess(abs(d['server_time']-time.time()),5)
  self.assertIn('next_change_at',d);self.assertNotIn('api_key',json.dumps(d))

class FeedbackTests(AuditTests):
 def test_async_violation_visible_only_to_owner_and_feedback_is_persisted(self):
  jid=self.waiting();self.cloud.audits.wechat_callback('trace-input','risky');self.wake(jid)
  j=self.client.get('/api/jobs/'+jid,headers=self.headers).json();self.assertEqual(j['violation']['violation_id'],jid)
  self.assertEqual(j['violation']['charged'],40)
  other={'Authorization':'Bearer '+m.user_token('other_user')}
  self.assertEqual(self.client.get('/api/jobs/'+jid,headers=other).status_code,404)
  self.assertEqual(self.client.post('/api/me/violation-feedback',headers=self.headers,json={'violation_id':jid,'message':'请复核'}).status_code,200)
  self.assertTrue(self.client.get('/api/jobs/'+jid,headers=self.headers).json()['violation']['feedback_submitted'])

 def test_expired_job_cleanup_forgets_audit_but_keeps_active_audit(self):
  jid=self.waiting();self.cloud.audits.wechat_callback('trace-input','pass');self.wake(jid)
  self.assertIsNotNone(self.cloud.audits.row(jid,'input'))
  m.jobs._on_evict=m._cleanup_job_files
  m.jobs.update(jid,status='failed',created_at=time.time()-2592001,completed_at=None);m.jobs.sweep()
  self.assertIsNone(self.cloud.audits.row(jid,'input'))

if __name__=='__main__':
 suite=unittest.TestSuite(cls(n) for cls in (PendingTests,FeedbackTests) for n in cls.__dict__ if n.startswith('test_'))
 names=[t._testMethodName for t in suite];r=unittest.TextTestRunner(verbosity=2).run(suite);failed={t._testMethodName for t,_ in r.failures+r.errors}
 (OUTPUT/'pending_fixes_results.json').write_text(json.dumps({'cases':[{'case':n,'passed':n not in failed} for n in names]},indent=2),encoding='utf-8')
 for c in initial_connections:
  try:c.close()
  except Exception:pass
 print('PENDING_FIXES_SUMMARY total=%d passed=%d failed=%d'%(r.testsRun,r.testsRun-len(failed),len(failed)))
 raise SystemExit(0 if r.wasSuccessful() else 1)
