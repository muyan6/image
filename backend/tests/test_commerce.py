"""Backend-only commerce settings with the released mini-program request contract."""
import copy,json,time,unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from test_virtual_payment import PaymentTests,m,OUTPUT,initial_connections,vp
from settings_store import SettingsStore
from credit_packages import default_commerce,public_packages,resolve_offer,amount_fen
import credit_packages as catalog

class CommerceTests(PaymentTests):
 def conf(self):return m.settings.snapshot()['commerce']
 def save(self,c):return m.settings.update({'commerce':c})
 def offer(self):return self.client.get('/api/credits/packages').json()['packages'][0]
 def sale(self,start=100,end=200):
  c=self.conf();c['packages'][0]['promotion'].update(enabled=True,starts_at=start,ends_at=end,amount_fen=480,points=800,product_id='holiday_600',description='假期优惠')
  self.save(c);return c

 def test_default_catalog_matches_released_client(self):
  rows=self.client.get('/api/credits/packages').json()['packages']
  self.assertEqual([(r['id'],r['price_text'],r['points']) for r in rows],
    [('points_600','¥6',600),('points_3300','¥30',3300),('points_8160','¥68',8160),('points_16640','¥128',16640)])
  self.assertTrue(all(set(['id','points','price_text','bonus_text','generations'])<=set(r) for r in rows))

 def test_new_user_reward_changes_without_recrediting_old_users(self):
  self.save({**self.conf(),'welcome_points':250})
  self.assertEqual(m._login_response('new_account_A',app_id='wxfixture')['balance'],250)
  self.save({**self.conf(),'welcome_points':500})
  self.assertEqual(m._login_response('new_account_A',app_id='wxfixture')['balance'],250)
  self.assertEqual(m._login_response('new_account_B',app_id='wxfixture')['balance'],500)
  self.assertEqual(m.users.get_balance('sample_user'),100)

 def test_zero_reward_and_concurrent_first_registration(self):
  self.save({**self.conf(),'welcome_points':0})
  self.assertEqual(m._login_response('new_zero')['balance'],0)
  with ThreadPoolExecutor(max_workers=4) as pool:
   results=list(pool.map(lambda _:m.users.ensure_user('new_concurrent',welcome_balance=300),range(8)))
  self.assertTrue(all(r['balance']==300 for r in results));self.assertEqual(m.users.get_balance('new_concurrent'),300)

 def test_old_settings_gain_defaults_without_resetting_existing_config(self):
  folder=self.d/'legacy';folder.mkdir();s=SettingsStore(str(folder));old=s.snapshot();old.pop('commerce');old['rewards']['invite']=77
  Path(s._path).write_text(json.dumps(old),encoding='utf-8');reopened=SettingsStore(str(folder))
  self.assertEqual(reopened.snapshot()['commerce'],default_commerce());self.assertEqual(reopened.rewards()['invite'],77)

 def test_settings_persist_new_reward_and_decimal_price(self):
  c=self.conf();c['welcome_points']=222;c['packages'][0]['amount_fen']=480;self.save(c)
  reopened=SettingsStore(str(self.d));self.assertEqual(reopened.snapshot()['commerce'],c)

 def test_fractional_prices_are_integer_cents_in_signed_order(self):
  c=self.conf();c['packages'][0].update(amount_fen=480,points=650,description='特惠 650 光子');self.save(c)
  shown=self.offer();self.assertEqual(shown['price_text'],'¥4.8');self.assertEqual(shown['points'],650)
  order=self.create(shown['id']);body=json.loads(order['pay_data']['signData'])
  self.assertEqual(body['goodsPrice'],480);self.assertIs(type(body['goodsPrice']),int)
  self.assertEqual(body['productId'],'points_600');self.assertEqual(order['order']['points'],650)

 def test_old_client_only_package_id_selects_new_offer(self):
  c=self.conf();c['packages'][0].update(amount_fen=888,points=1000);self.save(c);shown=self.offer()
  with patch.object(vp,'exchange_session',return_value={'openid':'sample_user','session_key':'fixture'}):
   r=self.client.post('/api/payment/orders',headers=self.headers,json={'package_id':shown['id'],'code':'fixture','client_key':'fixture_request_1'})
  self.assertEqual(r.status_code,200,r.text);self.assertEqual(r.json()['order']['amount'],888)

 def test_price_and_points_changes_reject_stale_checkout_ids(self):
  stale=self.offer()['id'];c=self.conf();c['packages'][0]['amount_fen']=480;self.save(c)
  with patch.object(vp,'exchange_session') as exchange:
   with self.assertRaisesRegex(ValueError,'重新进入'):self.create(stale)
  exchange.assert_not_called();self.assertEqual(m.payments.store().list('sample_user'),[])
  stale=self.offer()['id'];c['packages'][0]['points']=800;self.save(c)
  with self.assertRaises(ValueError):self.create(stale)

 def test_configuration_change_during_wechat_login_is_rechecked(self):
  shown=self.offer()['id']
  def exchange(*args):
   c=self.conf();c['packages'][0]['amount_fen']=480;self.save(c);return {'openid':'sample_user','session_key':'fixture'}
  with patch.object(vp,'exchange_session',side_effect=exchange):
   with self.assertRaisesRegex(ValueError,'更新'):m.payments.create(self.user,shown,'code','client_fixture_race')
  self.assertEqual(m.payments.store().list('sample_user'),[])

 def test_activity_start_and_end_boundaries_restore_normal_offer(self):
  c=self.sale();rows=c['packages']
  self.assertEqual(public_packages(catalog=rows,now=99)[0]['id'],'points_600')
  active=public_packages(catalog=rows,now=100)[0];self.assertEqual((active['price_text'],active['points']),('¥4.8',800))
  self.assertEqual(public_packages(catalog=rows,now=199)[0]['id'],active['id'])
  self.assertEqual(public_packages(catalog=rows,now=200)[0]['id'],'points_600')
  with self.assertRaises(ValueError):resolve_offer(rows,active['id'],now=200)

 def test_activity_uses_distinct_wechat_product_and_snapshots(self):
  now=int(time.time());self.sale(now-10,now+3600)
  created=self.create(self.offer()['id']);o=m.payments.store().get(created['order']['id'])
  self.assertEqual((o['package_id'],o['amount'],o['points']),('holiday_600',480,800))
  self.assertEqual(json.loads(o['sign_data'])['productId'],'holiday_600')
  c=self.conf();c['packages'][0]['promotion']['enabled']=False;self.save(c)
  with self.call(self.remote(o)):m.payments.sync(o['id'],True)
  self.assertEqual(m.users.get_balance('sample_user'),900)
  with self.call(self.remote(o,status=5,left=240)):m.payments.sync(o['id'],True)
  self.assertEqual(m.users.get_balance('sample_user'),500)

 def test_same_price_bonus_activity_is_supported(self):
  c=self.conf();c['packages'][0]['promotion'].update(enabled=True,starts_at=100,ends_at=200,points=1200)
  self.save(c);active=public_packages(catalog=c['packages'],now=150)[0]
  self.assertEqual(active['price_text'],'¥6');self.assertEqual(active['points'],1200)

 def test_disabled_packages_disappear_but_old_orders_still_reconcile(self):
  created=self.create();o=m.payments.store().get(created['order']['id'])
  c=self.conf();c['packages'][0]['enabled']=False;self.save(c)
  self.assertEqual(len(self.client.get('/api/credits/packages').json()['packages']),3)
  with self.assertRaises(ValueError):self.create(key='fixture_new_disabled')
  with self.call(self.remote(o)):m.payments.sync(o['id'],True)
  self.assertEqual(m.users.get_balance('sample_user'),700)

 def test_existing_order_idempotency_survives_offer_changes(self):
  original=self.create();c=self.conf();c['packages']=[];self.save(c)
  again=self.create();self.assertTrue(again['existing']);self.assertEqual(again['order']['id'],original['order']['id'])
  self.assertNotIn('pay_data',again);self.assertEqual(len(m.payments.store().list('sample_user')),1)

 def test_repeated_authoritative_delivery_adds_offer_points_once(self):
  c=self.conf();c['packages'][0].update(amount_fen=480,points=800);self.save(c)
  created=self.create(self.offer()['id']);o=m.payments.store().get(created['order']['id'])
  with self.call(self.remote(o)):
   m.payments.sync(o['id'],True);m.payments.sync(o['id'],True)
  self.assertEqual(m.users.get_balance('sample_user'),900)

 def test_admin_updates_only_commerce_and_preserves_other_settings(self):
  before=m.settings.snapshot();c=self.conf();c['welcome_points']=300
  r=self.admin.put('/admin/api/settings',json={'commerce':c});self.assertEqual(r.status_code,200,r.text)
  after=m.settings.snapshot();self.assertEqual(after['commerce']['welcome_points'],300)
  for key in before:
   if key not in ('commerce','cloud_mode_revision'):self.assertEqual(before[key],after[key],key)
  self.assertEqual(self.client.put('/admin/api/settings',json={'commerce':c}).status_code,403)

 def test_invalid_commerce_is_rejected_atomically(self):
  before=m.settings.snapshot()
  for update in [lambda c:c.update(welcome_points=-1),lambda c:c.update(welcome_points=True),
    lambda c:c['packages'][0].update(amount_fen=480.5),lambda c:c['packages'][0].update(amount_fen=0),
    lambda c:c['packages'][0].update(points=1.2),lambda c:c['packages'].append(copy.deepcopy(c['packages'][0]))]:
   c=self.conf();update(c)
   with self.assertRaises(ValueError):self.save(c)
   self.assertEqual(m.settings.snapshot(),before)

 def test_invalid_schedules_and_conflicting_wechat_prices_reject(self):
  for start,end,product in [(200,100,'sale'),(0,200,'sale'),(100,200,'points_600')]:
   c=self.conf();c['packages'][0]['promotion'].update(enabled=True,starts_at=start,ends_at=end,amount_fen=480,product_id=product)
   with self.assertRaises(ValueError):self.save(c)
  c=self.conf();c['packages'][1]['product_id']='points_600'
  with self.assertRaises(ValueError):self.save(c)

 def test_legacy_amount_conversion_is_exact(self):
  self.assertEqual(amount_fen({'yuan':4.8}),480)
  for value in [4.805,'NaN','Infinity',0,-1]:
   with self.assertRaises(ValueError):amount_fen({'yuan':value})

if __name__=='__main__':
 names=[n for n in CommerceTests.__dict__ if n.startswith('test_')]
 result=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(CommerceTests(n) for n in names))
 failed={t._testMethodName for t,_ in result.errors+result.failures}
 (OUTPUT/'commerce_results.json').write_text(json.dumps({'cases':[{'case':n,'passed':n not in failed} for n in names]},ensure_ascii=False,indent=2),encoding='utf-8')
 for c in initial_connections:
  try:c.close()
  except Exception:pass
 print('COMMERCE_SUMMARY total=%d passed=%d failed=%d'%(result.testsRun,result.testsRun-len(failed),len(failed)))
 raise SystemExit(0 if result.wasSuccessful() else 1)
