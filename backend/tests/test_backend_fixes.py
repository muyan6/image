"""Regression guards for the ten backend audit findings; isolated state, no external requests."""
import asyncio,json,os,sys,time,unittest,threading
from pathlib import Path
from unittest.mock import patch

ROOT=Path(os.environ.get("REVIEW_ROOT",Path(__file__).resolve().parents[2])).resolve()
OUT=Path(os.environ.get("REVIEW_OUTPUT",ROOT/"audit/backend-fix-tests")).resolve()
OUT.mkdir(parents=True,exist_ok=True)
os.environ.update(REVIEW_ROOT=str(ROOT),REVIEW_OUTPUT=str(OUT),PYTHONIOENCODING='utf-8')
sys.path.insert(0,str(ROOT/'backend/tests'))
from test_cloud_pipeline import CloudTests,m,cp,AsyncImages,initial_connections
from payment_store import PaymentStore
from template_output import select_template_output
from image_processing import QueueFull
import payment_api,cleanup_store,web_login,wechat_sec,tencent_cs
from gateway_ai import OpenAIImagesEnhance
from types import SimpleNamespace

findings=[]
def record(code,observed):
 findings.append({'id':code,'fixed':True,'observed':observed});print(code,json.dumps(observed,ensure_ascii=False),flush=True)

class BackendFixTests(CloudTests):
 def test_negative_balance_violation_preserves_refund_debt(self):
  s=PaymentStore(m.users);o,_=s.create('sample_user','fixture_checkout','fixture-app','fixture-offer',0,{'id':'points600','yuan':6,'points':600})
  s.apply_verified(o['id'],'fixture-wx',True,0)
  m.users.try_spend('sample_user',700)
  s.apply_verified(o['id'],'fixture-wx',True,600)
  before=m.users.get_balance('sample_user');self.assertEqual(before,-600)
  result=m.users.record_violation('sample_user','fixture-violation','text','fixture rejection',40)
  self.assertEqual(result['charged'],0);self.assertEqual(result['balance'],-600)
  record('B01',{'balance_before':before,'configured_penalty':40,'recorded_charge':result['charged'],'balance_after':result['balance']})

 def test_successful_cloud_job_survives_retention_error(self):
  jid=self.generating()
  with patch.object(m,'_retain_job_object',side_effect=RuntimeError('fixture transient retention write failure')):
   job=self.completed(jid)
  self.assertEqual(job['status'],'succeeded');self.assertEqual(m.users.get_balance('sample_user'),60)
  due=m.cleanup._conn.execute("SELECT due FROM cleanup WHERE kind='cos' AND target=?",(job['result_cos'],)).fetchone()[0]
  self.assertGreater(due,time.time()+86400)
  with patch.object(cleanup_store,'delete_object') as delete:
   m.cleanup.run(m.settings)
  targets=[call.args[1] for call in delete.call_args_list]
  self.assertNotIn(job['result_cos'],targets)
  self.cloud.step(jid)
  later=m.cleanup._conn.execute("SELECT due FROM cleanup WHERE kind='cos' AND target=?",(job['result_cos'],)).fetchone()[0]
  self.assertGreater(later,time.time()+86400)
  record('B02',{'status':job['status'],'balance_after':m.users.get_balance('sample_user'),'result_scheduled_for_immediate_deletion':False,'result_deleted_by_mock_cleanup':False,'charge_state':m.users._conn.execute('SELECT state FROM job_charges WHERE job_id=?',(jid,)).fetchone()[0]})

 def test_async_payment_callback_does_not_block_event_loop(self):
  class Request:
   method='POST';query_params={}
   async def stream(self):yield b'{"Event":"xpay_goods_deliver_notify"}'
  async def exercise():
   started=time.monotonic();loop_thread=threading.get_ident();worker_threads=[]
   def notification(body):
    worker_threads.append(threading.get_ident());time.sleep(.2)
   async def tick():await asyncio.sleep(.02);return time.monotonic()-started
   ticker=asyncio.create_task(tick());await asyncio.sleep(0)
   with patch.object(payment_api,'verify_push_signature',return_value=True),patch.object(m.payments,'notification',side_effect=notification):
    response=await payment_api.handle_message(m,Request())
   return await ticker,response.status_code,loop_thread,worker_threads[0]
  lag,status,loop_thread,worker_thread=asyncio.run(exercise());self.assertNotEqual(loop_thread,worker_thread);self.assertEqual(status,200)
  record('B03',{'notification_sync_delay_ms':200,'independent_timer_target_ms':20,'observed_timer_ms':round(lag*1000),'http_status':status})

 def test_single_output_preserves_noncomparison_landscape_size(self):
  m.settings.update({'free_mode':True})
  raw={'id':'fixture-landscape','name':'横向风景','prompt':'水平方向展开的水彩风景，保留自然色彩','text_fields':[],'layout':''}
  sizes={}
  for mode in ('template','single'):
   jid=self.new(template=select_template_output(raw,mode));self.step(jid,'prepare')
   with patch.object(AsyncImages,'submit',return_value='imgtask_fixture') as submit:self.step(jid,'submit')
   sizes[mode]=submit.call_args.args[3]
  self.assertEqual(sizes,{'template':'1536x1024','single':'1536x1024'})
  record('B04',{'input':'1536x1024, noncomparison landscape template','submitted_sizes':sizes})

 def test_moderation_service_exception_cleans_uploaded_temp_file(self):
  m.settings.update({'cloud_pipeline':{'enabled':False}})
  before=set(Path(m.UPLOAD_DIR).glob('incoming_*'))
  with patch.object(m,'_moderate_text_or_reject',side_effect=m.HTTPException(503,detail='fixture moderation unavailable')):
   r=self.client.post('/api/rescue',headers=self.headers,data={'custom_prompt':'增强清晰度'},files={'image':('fixture.jpg',self.image(),'image/jpeg')})
  after=set(Path(m.UPLOAD_DIR).glob('incoming_*'));left=after-before
  self.assertEqual(r.status_code,503);self.assertEqual(len(left),0)
  record('B05',{'http_status':r.status_code,'orphan_incoming_files':len(left),'orphan_bytes':sum(p.stat().st_size for p in left),'retention_seconds':m.ORIGINAL_TTL_SECONDS})

 def test_rejected_text_queue_does_not_consume_daily_submission_quota(self):
  m.settings.update({'cloud_pipeline':{'enabled':False},'text_generation':{'enabled':True,'model':'fixture','price':40,'base_url':'https://fixture.invalid','api_key':'fixture'},'quota':{'daily':1,'per_minute':1000}})
  with patch.object(m,'_moderate_text_or_reject',return_value=None),patch.object(m.pool,'submit',side_effect=QueueFull('fixture queue full')):
   r=self.client.post('/api/text-generation',headers=self.headers,json={'prompt':'水彩森林'})
   follow=self.client.post('/api/text-generation',headers=self.headers,json={'prompt':'水彩森林'})
  self.assertEqual(r.status_code,429);self.assertEqual(m.users.get_balance('sample_user'),100)
  count=m.users._conn.execute("SELECT count(*) FROM audit WHERE openid='sample_user' AND action='submitted'").fetchone()[0]
  self.assertEqual(count,0);self.assertIn('queue full',follow.json()['detail']);self.assertEqual(m.users.get_user('sample_user')['total_jobs'],0)
  record('B06',{'first_http_status':r.status_code,'balance_refunded':100,'retained_submitted_audit_rows':count,'total_jobs':m.users.get_user('sample_user')['total_jobs'],'next_request_detail':follow.json()['detail']})

 def test_origin_port_zero_is_rejected(self):
  value=web_login.normalized_origin('https://fixture.invalid:0')
  self.assertIsNone(value)
  record('B07',{'input':'https://fixture.invalid:0','normalized_origin':value,'default_origin':web_login.normalized_origin('https://fixture.invalid')})

 def test_gateway_download_keeps_credentials_off_other_hosts(self):
  client=OpenAIImagesEnhance({'base_url':'https://gateway.invalid','api_key':'fixture-not-a-real-key'})
  calls=[]
  def get(url,**kwargs):
   calls.append({'url':url,**kwargs})
   return SimpleNamespace(status_code=403,content=b'fixture',raise_for_status=lambda:None)
  client._session=SimpleNamespace(get=get)
  from gateway_ai import GatewayError
  with self.assertRaises(GatewayError):client._download('https://different-image-host.invalid/result.jpg')
  self.assertEqual(len(calls),1);self.assertNotIn('headers',calls[0])
  record('B08',{'cross_origin_requests':len(calls),'api_key_forwarded':False})

 def test_wechat_token_cache_is_bound_to_app_credentials(self):
  m.settings.update({'wechat':{'app_id':'fixture-app-B','app_secret':'fixture-secret-B'}})
  response=SimpleNamespace(json=lambda:{'access_token':'fixture-token-B','expires_in':7200})
  with patch.dict(wechat_sec._token_cache,{'token':'fixture-token-A','expires_at':time.time()+3600},clear=True),patch.object(wechat_sec.requests,'post',return_value=response) as post:
   token=wechat_sec.get_access_token(m.settings)
   again=wechat_sec.get_access_token(m.settings)
   self.assertEqual(token,'fixture-token-B');self.assertEqual(again,token);self.assertEqual(post.call_count,1)
   m.settings.update({'wechat':{'app_secret':'new-fixture-secret'}})
   wechat_sec.get_access_token(m.settings);self.assertEqual(post.call_count,2)
  record('B09',{'new_app_token_fetched':True,'same_credentials_cached':True,'secret_change_refreshed':True})

 def test_malformed_moderation_responses_are_service_errors(self):
  m.settings.update({'moderation':{'enabled':True,'block_on_error':True}})
  response=SimpleNamespace(status_code=200,json=lambda:{},raise_for_status=lambda:None)
  with patch.object(tencent_cs.requests,'post',return_value=response),patch.object(wechat_sec,'wechat_text_ready',return_value=False):
   with self.assertRaises(tencent_cs.ModerationError):tencent_cs.moderate_image_bytes(b'fixture',m.settings)
   with self.assertRaises(m.HTTPException) as caught:m._moderate_text_or_reject('正常描述','sample_user')
  self.assertEqual(caught.exception.status_code,503);self.assertEqual(m.users.get_balance('sample_user'),100)
  self.assertEqual(len(m.users.list_violations()),0)
  record('B10',{'image_not_passed':True,'text_http_status':503,'balance_after':100,'violations':0})

 def test_penalties_and_overturn_preserve_debt_and_cap_available_balance(self):
  for index,(balance,price,charge) in enumerate([(-600,0,0),(-600,40,0),(0,40,0),(20,40,20),(100,40,40)]):
   with self.subTest(balance=balance,price=price):
    with m.users._lock,m.users._conn:m.users._conn.execute('UPDATE users SET balance=? WHERE openid=?',(balance,'sample_user'))
    vid='test_penalty_'+str(index);event=m.users.record_violation('sample_user',vid,'text','fixture',price)
    self.assertEqual(event['charged'],charge);self.assertEqual(event['balance'],balance-charge)
    m.users.resolve_violation(vid,True);self.assertEqual(m.users.get_balance('sample_user'),balance)

 def test_cancelled_audits_are_job_scoped_and_idempotent(self):
  for jid,label in [('abc123','文字生图'),('abc456','云端异步生成 fixture')]:
   m.users.reserve_job('sample_user',jid,10,{});m.users.confirm_job(jid,label)
  m.users.refund_job('sample_user','abc123',cancel=True);m.users.refund_job('sample_user','abc123',cancel=True)
  self.assertEqual(m.users.get_balance('sample_user'),90);self.assertEqual(m.users.get_user('sample_user')['total_jobs'],1)
  rows=m.users._conn.execute("SELECT detail FROM audit WHERE action='submitted'").fetchall()
  self.assertEqual(len(rows),1);self.assertTrue(rows[0][0].startswith('job=abc456 '))

 def test_old_queued_single_snapshot_keeps_original_ratio(self):
  tpl=select_template_output({'id':'landscape','prompt':'水彩山水','layout':'','text_fields':[]},'single');tpl.pop('layout_prompt')
  jid=self.new(template=tpl);self.step(jid,'prepare')
  with patch.object(AsyncImages,'submit',return_value='imgtask_fixture') as submit:self.step(jid,'submit')
  self.assertEqual(submit.call_args.args[3],'1536x1024')

 def test_portrait_template_layout_still_requests_portrait(self):
  tpl=select_template_output({'id':'portrait','prompt':'竖版水彩肖像','layout':'','text_fields':[]},'single')
  jid=self.new(template=tpl);self.step(jid,'prepare')
  with patch.object(AsyncImages,'submit',return_value='imgtask_fixture') as submit:self.step(jid,'submit')
  self.assertEqual(submit.call_args.args[3],'1024x1536')

 def test_cos_upload_moderation_error_cleans_host_temp_file(self):
  m.settings.update({'cloud_pipeline':{'enabled':False}})
  m._uploads['fixture_upload']={**self.source,'created_at':time.time()}
  before=set(Path(m.UPLOAD_DIR).glob('incoming_*'))
  with patch.object(m,'cos_head',return_value=True),patch.object(m,'cos_get',return_value=self.image()),patch.object(m,'_moderate_text_or_reject',side_effect=m.HTTPException(503,detail='fixture unavailable')):
   r=self.client.post('/api/rescue/by-upload',headers=self.headers,json={'upload_id':'fixture_upload','custom_prompt':'清晰'})
  self.assertEqual(r.status_code,503);self.assertEqual(set(Path(m.UPLOAD_DIR).glob('incoming_*')),before)

 def test_gateway_same_origin_auth_retry_disallows_redirects(self):
  client=OpenAIImagesEnhance({'base_url':'https://gateway.invalid','api_key':'fixture-key'});calls=[]
  def get(url,**kw):
   calls.append(kw);return SimpleNamespace(status_code=403 if len(calls)==1 else 200,url=url,content=b'image',raise_for_status=lambda:None)
  client._session=SimpleNamespace(get=get)
  self.assertEqual(client._download('https://gateway.invalid:443/result'),'image'.encode())
  self.assertEqual(calls[1]['headers']['Authorization'],'Bearer fixture-key');self.assertFalse(calls[1]['allow_redirects'])

 def test_gateway_public_redirect_to_other_host_never_receives_key(self):
  from gateway_ai import GatewayError
  client=OpenAIImagesEnhance({'base_url':'https://gateway.invalid','api_key':'fixture-key'})
  with patch.object(client._session,'get',return_value=SimpleNamespace(status_code=403,url='https://other.invalid/image',content=b'')) as get:
   with self.assertRaises(GatewayError):client._download('https://gateway.invalid/image')
  self.assertEqual(get.call_count,1);self.assertNotIn('headers',get.call_args.kwargs)

 def test_malformed_audit_payload_variants_never_become_verdicts(self):
  bodies=[[],{}, {'Response':None},{'Response':[]},{'Response':{}},{'Response':{'Error':'bad'}},
    {'Response':{'Suggestion':'unknown'}},{'Response':{'Suggestion':'Pass','Score':'bad'}}]
  for body in bodies:
   with self.subTest(body=body),patch.object(tencent_cs.requests,'post',return_value=SimpleNamespace(status_code=200,json=lambda:body,raise_for_status=lambda:None)):
    with self.assertRaises(tencent_cs.ModerationError):tencent_cs.moderate_image_bytes(b'fixture',m.settings)
    with self.assertRaises(tencent_cs.ModerationError):tencent_cs.moderate_text('normal',m.settings)

 def test_valid_audit_verdicts_are_preserved(self):
  for verdict in ('Pass','Review','Block'):
   response=SimpleNamespace(status_code=200,json=lambda:{'Response':{'Suggestion':verdict,'Label':'Normal','Score':30}},raise_for_status=lambda:None)
   with patch.object(tencent_cs.requests,'post',return_value=response):
    self.assertEqual(tencent_cs.moderate_image_bytes(b'fixture',m.settings)[0],verdict)
    self.assertEqual(tencent_cs.moderate_text('normal',m.settings)[0],verdict)

if __name__=='__main__':
 suite=unittest.TestSuite(BackendFixTests(n) for n in BackendFixTests.__dict__ if n.startswith('test_'))
 result=unittest.TextTestRunner(verbosity=2).run(suite)
 failed={t._testMethodName for t,_ in result.failures+result.errors}
 (OUT/'backend_fixes_results.json').write_text(json.dumps({'cases':[{'case':n,'passed':n not in failed} for n in BackendFixTests.__dict__ if n.startswith('test_')],'observations':findings},ensure_ascii=False,indent=2),encoding='utf-8')
 for connection in initial_connections:
  try:connection.close()
  except Exception:pass
 print('BACKEND_FIXES_SUMMARY total=%d observed=%d failed=%d'%(result.testsRun,len(findings),len(result.errors)+len(result.failures)))
 raise SystemExit(0 if result.wasSuccessful() else 1)
