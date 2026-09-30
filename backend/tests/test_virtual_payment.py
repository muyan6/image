"""Financial invariants with isolated SQLite and mocked WeChat; no money movement."""
import json,time,unittest,hashlib
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from test_workflow import WorkflowTests,m,OUTPUT,initial_connections
import virtual_payment as vp
from payment_store import PaymentStore
from payment_api import parse_message

class PaymentTests(WorkflowTests):
 def setUp(self):
  super().setUp();m.settings.update({'payment':{'enabled':True,'env':0,'offer_id':'1450665368','production_app_key':'fixture-appkey'},
    'wechat':{'app_id':'wxfixture','app_secret':'fixture'},'moderation':{'wechat_push_token':'fixture-token'}})
  self.user={'openid':'sample_user','account_type':'wechat','banned':False};self.calls=[]
 def create(self,package='points_600',key='client_fixture_1234'):
  with patch.object(vp,'exchange_session',return_value={'openid':'sample_user','session_key':'session-fixture'}):
   return m.payments.create(self.user,package,'fixture-code',key)
 def remote(self,o,status=2,left=None):
  return {'errcode':0,'order':{'order_id':o['id'],'status':status,'order_type':0,'order_fee':o['amount'],
    'paid_fee':o['amount'],'env_type':o['env']+1,'wx_order_id':'WX'+o['id'],'left_fee':o['amount'] if left is None else left}}
 def call(self,remote):
  def run(path,body,o):self.calls.append((path,body));return remote if path=='/xpay/query_order' else {}
  return patch.object(m.payments,'call',side_effect=run)
 def test_pay_disabled_and_unknown_package_do_not_create_order(self):
  m.settings.update({'payment':{'enabled':False}})
  with self.assertRaises(ValueError):self.create()
  self.assertEqual(m.payments.store().list('sample_user'),[])
  m.settings.update({'payment':{'enabled':True}})
  with self.assertRaises(ValueError):self.create('bogus')
 def test_pay_signatures_exact_string_and_no_secret_response(self):
  d=self.create();o=m.payments.store().get(d['order']['id']);p=d['pay_data']
  self.assertEqual(p['paySig'],vp.signature('fixture-appkey','requestVirtualPayment&'+p['signData']))
  self.assertEqual(p['signature'],vp.signature('session-fixture',p['signData']))
  self.assertNotIn('fixture-appkey',json.dumps(d));self.assertNotIn('session-fixture',json.dumps(d))
  self.assertEqual(json.loads(p['signData'])['goodsPrice'],600);self.assertEqual(o['points'],600)
  self.assertEqual(m.users.get_balance('sample_user'),100)
 def test_pay_matches_official_signature_vectors(self):
  raw='{"openid": "xxx", "user_ip": "127.0.0.1", "env": 0}'
  self.assertEqual(vp.signature('12345','/xpay/query_user_balance&'+raw),'c37809f27c6d7fd1837ad2500a04512b66b34fd793a39a385fade56dca89a4b5')
  self.assertEqual(vp.signature('9hAb/NEYUlkaMBEsmFgzig==',raw),'089d9e8dc5d308977360c4b79ec600a93d736802802a807d634192328032f6c7')
 def test_pay_HTTP_query_body_matches_signed_bytes(self):
  from unittest.mock import Mock
  o=m.payments.store().get(self.create()['order']['id']);body={'openid':'sample_user','env':0,'order_id':o['id']}
  response=Mock(status_code=200);response.json.return_value=self.remote(o)
  with patch.object(vp,'get_access_token',return_value='fixture-token'),patch.object(vp.requests,'post',return_value=response) as post:
   m.payments.call('/xpay/query_order',body,o)
  sent=post.call_args.kwargs
  self.assertEqual(sent['params']['pay_sig'],vp.signature('fixture-appkey','/xpay/query_order&'+sent['data'].decode()))
  self.assertFalse(sent['allow_redirects'])
 def test_pay_all_four_packages_server_prices_and_points(self):
  for i,p in enumerate(vp.PACKAGES):
   d=self.create(p['id'],'client_fixture_1234'+str(i));raw=json.loads(d['pay_data']['signData'])
   self.assertEqual(raw['goodsPrice'],p['yuan']*100);self.assertEqual(d['order']['points'],p['points']);self.assertEqual(raw['buyQuantity'],1)
 def test_pay_idempotent_creation_does_not_reuse_payment_signature(self):
  a=self.create();b=self.create();self.assertEqual(a['order']['id'],b['order']['id']);self.assertTrue(b['existing']);self.assertNotIn('pay_data',b)
 def test_pay_account_mismatch_and_web_account_blocked(self):
  with patch.object(vp,'exchange_session',return_value={'openid':'other_user','session_key':'secret'}):
   with self.assertRaises(ValueError):m.payments.create(self.user,'points_600','code','client_fixture_1234')
  with self.assertRaises(ValueError):m.payments.create({**self.user,'account_type':'web'},'points_600','code','client_fixture_1234')
 def test_pay_paid_query_credits_exactly_once_and_notifies_delivery(self):
  d=self.create();o=m.payments.store().get(d['order']['id'])
  with self.call(self.remote(o)):
   m.payments.sync(o['id'],True);m.payments.sync(o['id'],True)
  self.assertEqual(m.users.get_balance('sample_user'),700)
  self.assertEqual(m.payments.store().get(o['id'])['status'],'delivered')
  self.assertEqual(sum(p=='/xpay/notify_provide_goods' for p,b in self.calls),1)
 def test_pay_unpaid_client_success_never_credits(self):
  d=self.create();o=m.payments.store().get(d['order']['id'])
  with self.call(self.remote(o,status=1)):m.payments.sync(o['id'],True)
  self.assertEqual(m.users.get_balance('sample_user'),100)
 def test_pay_mismatched_amount_environment_and_order_never_credit(self):
  d=self.create();o=m.payments.store().get(d['order']['id'])
  for k,value in [('order_id','other'),('order_fee',1),('paid_fee',0),('env_type',2),('order_type',1),('wx_order_id','')]:
   r=self.remote(o);r['order'][k]=value
   with self.call(r):
    with self.assertRaises(ValueError):m.payments.sync(o['id'],True)
   self.assertEqual(m.users.get_balance('sample_user'),100)
 def test_pay_forged_callback_requires_authoritative_query(self):
  d=self.create();o=m.payments.store().get(d['order']['id']);body={'Event':'xpay_goods_deliver_notify','OpenId':o['openid'],'OutTradeNo':o['id'],'Env':0,
    'GoodsInfo':{'ProductId':o['package_id'],'Quantity':1,'OrigPrice':600,'ActualPrice':600}}
  with self.call(self.remote(o,status=1)):
   with self.assertRaises(ValueError):m.payments.notification(body)
  self.assertEqual(m.users.get_balance('sample_user'),100)
 def test_pay_wrong_goods_owner_and_quantity_reject_before_query(self):
  d=self.create();o=m.payments.store().get(d['order']['id']);body={'Event':'xpay_goods_deliver_notify','OpenId':'other_user','OutTradeNo':o['id'],'Env':0,
    'GoodsInfo':{'ProductId':o['package_id'],'Quantity':1,'OrigPrice':600,'ActualPrice':600}}
  with patch.object(m.payments,'call') as call:
   with self.assertRaises(ValueError):m.payments.notification(body)
   call.assert_not_called()
 def test_pay_refund_full_partial_and_repeated_notifications(self):
  d=self.create();o=m.payments.store().get(d['order']['id'])
  with self.call(self.remote(o)):m.payments.sync(o['id'],True)
  with self.call(self.remote(o,status=5,left=300)):
   m.payments.sync(o['id'],True);m.payments.sync(o['id'],True)
  self.assertEqual(m.users.get_balance('sample_user'),400)
  with self.call(self.remote(o,status=5,left=0)):m.payments.sync(o['id'],True)
  self.assertEqual(m.users.get_balance('sample_user'),100)
  with self.call(self.remote(o)):m.payments.sync(o['id'],True)
  self.assertEqual(m.users.get_balance('sample_user'),100)
 def test_pay_refund_before_delivery_never_adds_points(self):
  d=self.create();o=m.payments.store().get(d['order']['id'])
  with self.call(self.remote(o,status=8,left=0)):m.payments.sync(o['id'],True)
  self.assertEqual(m.users.get_balance('sample_user'),100)
 def test_pay_partial_refund_before_delivery_grants_only_net_entitlement(self):
  d=self.create();o=m.payments.store().get(d['order']['id'])
  with self.call(self.remote(o,status=5,left=300)):m.payments.sync(o['id'],True)
  self.assertEqual(m.users.get_balance('sample_user'),400)
 def test_pay_refund_keeps_spent_credit_debt(self):
  d=self.create();o=m.payments.store().get(d['order']['id'])
  with self.call(self.remote(o)):m.payments.sync(o['id'],True)
  m.users.try_spend('sample_user',650)
  with self.call(self.remote(o,status=5,left=0)):m.payments.sync(o['id'],True)
  self.assertEqual(m.users.get_balance('sample_user'),-550)
 def test_pay_platform_order_id_cannot_be_reused_for_other_order(self):
  a=self.create()['order']['id'];b=self.create(key='client_fixture_5678')['order']['id'];s=m.payments.store()
  s.apply_verified(a,'unique-wxid',True)
  with self.assertRaises(Exception):s.apply_verified(b,'unique-wxid',True)
  self.assertEqual(m.users.get_balance('sample_user'),700)
 def test_pay_concurrent_delivery_atomic_and_restart_durable(self):
  d=self.create();o=m.payments.store().get(d['order']['id']);s=m.payments.store()
  with ThreadPoolExecutor(max_workers=8) as pool:list(pool.map(lambda _:s.apply_verified(o['id'],'WX'+o['id'],True),range(20)))
  self.assertEqual(m.users.get_balance('sample_user'),700)
  reopened=PaymentStore(m.users);self.assertEqual(reopened.get(o['id'])['credited'],1)
  self.assertEqual(m.users._conn.execute("SELECT COUNT(*) FROM payment_ledger WHERE action='credit'").fetchone()[0],1)
 def test_pay_order_ownership_and_no_public_secret_exposure(self):
  d=self.create();jid=d['order']['id']
  self.assertEqual(self.client.get('/api/payment/orders/'+jid).status_code,401)
  h={'Authorization':'Bearer '+m.user_token('other_user')};self.assertEqual(self.client.get('/api/payment/orders/'+jid,headers=h).status_code,404)
  r=self.client.get('/api/payment/orders',headers=self.headers);self.assertEqual(r.status_code,200)
  for text in ('fixture-appkey','session-fixture','sign_data','openid'):self.assertNotIn(text,r.text)
 def test_pay_price_tampering_ignored_by_server_checkout(self):
  m.users._conn.execute("UPDATE users SET account_type='wechat' WHERE openid='sample_user'");m.users._conn.commit()
  with patch.object(vp,'exchange_session',return_value={'openid':'sample_user','session_key':'session-fixture'}):
   r=self.client.post('/api/payment/orders',headers=self.headers,json={'package_id':'points_600','code':'code','client_key':'client_fixture_1234','points':99999,'amount':1})
  self.assertEqual(r.status_code,200,r.text);self.assertEqual(r.json()['order']['amount'],600)
 def test_pay_XML_JSON_and_entity_rejection(self):
  self.assertEqual(parse_message(b'<xml><Event>xpay_goods_deliver_notify</Event><GoodsInfo><Quantity>1</Quantity></GoodsInfo></xml>')['GoodsInfo']['Quantity'],'1')
  self.assertEqual(parse_message(b'{"Event":"event"}')['Event'],'event')
  for raw in (b'<!DOCTYPE xml><xml/>',b'{"Encrypt":"cipher"}'):
   with self.assertRaises(ValueError):parse_message(raw)
 def test_pay_unsigned_push_rejected_and_signed_unpaid_push_not_acknowledged(self):
  d=self.create();o=m.payments.store().get(d['order']['id'])
  body={'Event':'xpay_goods_deliver_notify','OpenId':'sample_user','OutTradeNo':o['id'],'Env':0,'GoodsInfo':{'ProductId':'points_600','Quantity':1,'OrigPrice':600,'ActualPrice':600}}
  self.assertEqual(self.client.post('/api/payment/notify',json=body).status_code,403)
  ts='123';nonce='fixture';sig=hashlib.sha1(''.join(sorted(['fixture-token',ts,nonce])).encode()).hexdigest()
  with self.call(self.remote(o,status=1)):
   r=self.client.post('/api/wxpush?timestamp='+ts+'&nonce='+nonce+'&signature='+sig,json=body)
  self.assertNotEqual(r.json()['ErrCode'],0);self.assertEqual(m.users.get_balance('sample_user'),100)

if __name__=='__main__':
 suite=unittest.TestSuite(PaymentTests(n) for n in PaymentTests.__dict__ if n.startswith('test_pay_'))
 names=[t._testMethodName for t in suite];result=unittest.TextTestRunner(verbosity=2).run(suite)
 failed={t._testMethodName for t,_ in result.failures+result.errors}
 (OUTPUT/'virtual_payment_results.json').write_text(json.dumps({'cases':[{'case':n,'passed':n not in failed} for n in names]},indent=2),encoding='utf-8')
 for c in initial_connections:
  try:c.close()
  except Exception:pass
 print('VIRTUAL_PAYMENT_SUMMARY total=%d passed=%d failed=%d'%(result.testsRun,result.testsRun-len(failed),len(failed)))
 raise SystemExit(0 if result.wasSuccessful() else 1)
