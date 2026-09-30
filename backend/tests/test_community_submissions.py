"""Offline end-to-end community lifecycle, privacy and same-ledger reward checks."""
import json,time,unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from test_cloud_pipeline import CloudTests,m,cp,OUTPUT,initial_connections
from community_submissions import store_for,SubmissionStore,PENDING_TTL
from community_api import maintain


class SubmissionTests(CloudTests):
 def setUp(self):
  super().setUp();self.media={};self.extra=[];self.copy_calls=[]
  self.job_id='abc123abc123';self.make_job(self.job_id)
  def head(settings,key):
   if key not in self.media:raise cp.cos.CosError('not found',status=404)
   return {'size':self.media[key],'content_type':'image/jpeg'}
  def copy(settings,source,target,**kw):
   head(settings,source);self.media[target]=self.media[source];self.copy_calls.append((source,target,kw))
  for obj,name,fn in [(cp.cos,'object_metadata',head),(cp.cos,'copy_object',copy),(m,'_moderate_text_or_reject',lambda *a:None)]:
   p=patch.object(obj,name,side_effect=fn);self.extra.append(p);p.start()
  p=patch('cleanup_store.delete_object',side_effect=lambda settings,key,**kw:self.media.pop(key,None));self.extra.append(p);p.start()
  m.users.set_nickname('sample_user','真实作者')
  self.store=store_for(m.users)
 def tearDown(self):
  for p in reversed(self.extra):p.stop()
  super().tearDown()
 def make_job(self,jid,owner='sample_user',**fields):
  result='results/private/'+jid+'.jpg';original='origins/private/'+jid+'.png'
  m.jobs.create(jid,openid=owner,result_cos=result,orig_cos=original,quality='light',template_id='t_fixture',template_name='胶片')
  m.jobs.update(jid,status='succeeded',completed_at=time.time(),**fields);self.media.update({result:1000,original:1200})
 def post(self,**fields):
  return self.client.post('/api/community/submissions',headers=self.headers,json={'job_id':self.job_id,'title':'我的作品','story':'创作故事','consent':True,**fields})
 def submit(self,**fields):
  r=self.post(**fields);self.assertEqual(r.status_code,200,r.text);return r.json()['submission']
 def review(self,p,action='approve',**kw):
  return self.admin.post('/admin/api/community/submissions/'+p['id']+'/review',json={'revision':p['revision'],'action':action,**kw})
 def feed(self,headers=None):return self.client.get('/api/community',headers=headers or {}).json()['items']
 def withdraw(self,p,headers=None):return self.client.post('/api/community/submissions/'+p['id']+'/withdraw',headers=headers or self.headers,json={'revision':p['revision']})

 def test_result_only_is_pending_private_and_not_auto_published(self):
  p=self.submit();self.assertEqual(p['status'],'pending');self.assertFalse(p['share_original']);self.assertEqual(p['orig_url'],'')
  self.assertEqual(self.feed(),[]);self.assertEqual(len(self.copy_calls),1);self.assertTrue(self.copy_calls[0][2]['private'])
  self.assertTrue(self.copy_calls[0][1].startswith('community/submissions/'));self.assertEqual(m.users.get_balance('sample_user'),100)

 def test_explicit_original_consent_copies_only_authorized_original(self):
  p=self.submit(share_original=True);self.assertEqual(len(self.copy_calls),2);self.assertIn('/community/submissions/',p['orig_url'])
  self.assertEqual(self.review(p).status_code,200);row=self.feed()[0]
  self.assertTrue(row['origUrl']);self.assertNotIn('/origins/private/',json.dumps(row));self.assertNotIn('openid',row)

 def test_missing_or_non_boolean_consent_is_rejected_before_copy(self):
  for value in (False,'true',1):self.assertIn(self.post(consent=value).status_code,(400,422))
  self.assertEqual(self.copy_calls,[])

 def test_foreign_unfinished_deleted_and_expired_jobs_are_not_submittable(self):
  self.make_job('def456def456','other_user');self.assertEqual(self.post(job_id='def456def456').status_code,404)
  for fields in ({'status':'processing'},{'status':'succeeded','deleted_at':time.time()},{'status':'succeeded','deleted_at':None,'completed_at':time.time()-m.JOB_TTL_SECONDS-1}):
   m.jobs.update(self.job_id,**fields);self.assertIn(self.post().status_code,(404,409))
  self.assertEqual(self.copy_calls,[])

 def test_text_image_cannot_claim_original(self):
  m.jobs.update(self.job_id,orig_cos=None,input_mode='text')
  self.assertEqual(self.post(share_original=True).status_code,400);self.assertEqual(self.submit()['status'],'pending')

 def test_client_cannot_choose_image_author_status_or_reward(self):
  p=self.submit(result_key='evil',result_url='https://evil.invalid',status='published',author_name='伪造作者',reward=9999)
  self.assertEqual(p['author_name'],'真实作者');self.assertEqual(p['status'],'pending');self.assertEqual(p['reward'],0)
  self.assertEqual(self.copy_calls[0][0],'results/private/'+self.job_id+'.jpg')

 def test_duplicate_submit_reuses_one_record_without_copy_or_reward(self):
  a=self.submit();b=self.submit();self.assertEqual(a['id'],b['id']);self.assertEqual(len(self.copy_calls),1)
  self.assertEqual(self.store.list(owner='sample_user')[1],1);self.assertEqual(m.users.get_balance('sample_user'),100)

 def test_pending_content_is_immutable_until_withdrawal(self):
  self.submit();self.assertEqual(self.post(title='修改标题').status_code,409)

 def test_copy_failure_can_resume_same_submission(self):
  with patch.object(cp.cos,'copy_object',side_effect=cp.cos.CosError('temporary',code='NETWORK')):r=self.post()
  self.assertEqual(r.status_code,503);row=self.store.list(owner='sample_user')[0][0];self.assertEqual(row['status'],'uploading')
  p=self.submit();self.assertEqual(p['id'],row['id']);self.assertEqual(p['status'],'pending')

 def test_owner_list_never_includes_other_users_pending_media(self):
  p=self.submit();other={'Authorization':'Bearer '+m.user_token('other_user')}
  self.assertEqual(self.client.get('/api/community/submissions/mine',headers=other).json()['items'],[])
  self.assertEqual(self.withdraw(p,other).status_code,404)

 def test_unapproved_rejected_and_withdrawn_do_not_appear_publicly(self):
  p=self.submit();self.assertEqual(self.review(p,'reject',reason='请调整内容').status_code,200);self.assertEqual(self.feed(),[])
  p=self.submit();self.assertEqual(p['revision'],2);self.assertEqual(self.review(p).status_code,200);self.assertEqual(len(self.feed()),1)
  self.assertEqual(self.withdraw(p).status_code,200);self.assertEqual(self.feed(),[])

 def test_admin_auth_is_required_for_review_and_rewards(self):
  p=self.submit();r=self.client.post('/admin/api/community/submissions/'+p['id']+'/review',headers=self.headers,json={'revision':1,'action':'approve'})
  self.assertEqual(r.status_code,403);self.assertEqual(self.store.get(p['id'])['status'],'pending')

 def test_approval_retains_copies_but_does_not_award_points(self):
  p=self.submit();self.assertEqual(self.review(p).status_code,200);row=self.store.get(p['id'])
  for key in (row['payload']['result_key'],):self.assertIsNone(m.cleanup._conn.execute('SELECT due FROM cleanup WHERE target=?',(key,)).fetchone())
  self.assertEqual(m.users.get_balance('sample_user'),100)

 def test_feature_reward_is_same_transaction_and_exactly_once(self):
  p=self.submit();self.review(p);m.settings.update({'rewards':{'community_featured':75}})
  self.assertEqual(self.review(p,'feature').status_code,200);self.assertEqual(self.review(p,'feature').status_code,200)
  self.assertEqual(m.users.get_balance('sample_user'),175);self.assertEqual(self.store.get(p['id'])['reward'],75)
  self.assertEqual(m.users._conn.execute('SELECT COUNT(*) FROM community_rewards').fetchone()[0],1)

 def test_concurrent_feature_operations_award_only_once(self):
  p=self.submit();self.review(p)
  with ThreadPoolExecutor(max_workers=6) as pool:list(pool.map(lambda _:self.store.review(p['id'],p['revision'],'feature',reward=50),range(12)))
  self.assertEqual(m.users.get_balance('sample_user'),150)

 def test_reward_rolls_back_with_database_failure(self):
  p=self.submit();self.review(p)
  m.users._conn.execute("CREATE TRIGGER fail_reward BEFORE UPDATE OF balance ON users BEGIN SELECT RAISE(ABORT,'fixture failure'); END");m.users._conn.commit()
  with self.assertRaises(Exception):self.store.review(p['id'],p['revision'],'feature',reward=50)
  self.assertEqual(m.users._conn.execute('SELECT COUNT(*) FROM community_rewards').fetchone()[0],0)
  self.assertFalse(self.store.get(p['id'])['featured']);self.assertEqual(m.users.get_balance('sample_user'),100)

 def test_unfeature_and_repost_never_reaward(self):
  p=self.submit();self.review(p);self.review(p,'feature');self.review(p,'unfeature');self.review(p,'feature');self.withdraw(p)
  p=self.submit(share_original=True);self.review(p);self.review(p,'feature')
  self.assertEqual(m.users.get_balance('sample_user'),150);self.assertEqual(self.store.get(p['id'])['reward'],50)

 def test_stale_admin_revision_cannot_approve_changed_consent(self):
  a=self.submit();self.withdraw(a);b=self.submit(share_original=True)
  self.assertEqual(self.review(a).status_code,409);self.assertEqual(self.store.get(b['id'])['status'],'pending')

 def test_withdraw_during_copy_does_not_resurrect_or_publish(self):
  original=cp.cos.copy_object.side_effect
  def copy(settings,source,target,**kw):
   original(settings,source,target,**kw);row=self.store.list(owner='sample_user')[0][0]
   self.store.withdraw(row['id'],'sample_user',row['revision'])
  with patch.object(cp.cos,'copy_object',side_effect=copy):r=self.post()
  self.assertEqual(r.status_code,409);self.assertEqual(self.store.list(owner='sample_user')[0][0]['status'],'withdrawn');self.assertEqual(self.feed(),[])

 def test_work_deletion_does_not_break_published_independent_copy(self):
  p=self.submit();self.review(p);key=self.store.get(p['id'])['payload']['result_key']
  self.assertEqual(self.client.delete('/api/my/jobs/'+self.job_id,headers=self.headers).status_code,200)
  self.assertIn(key,self.media);self.assertEqual(len(self.feed()),1)

 def test_likes_are_persistent_owner_unique_and_idempotent(self):
  p=self.submit();self.review(p);url='/api/community/posts/'+p['id']+'/like'
  for _ in range(2):self.assertEqual(self.client.put(url,headers=self.headers,json={'liked':True}).json()['likes'],1)
  self.assertTrue(self.feed(self.headers)[0]['liked']);self.assertFalse(self.feed()[0]['liked'])
  self.assertEqual(SubmissionStore(m.users).likes([p['id']],'sample_user')[p['id']],(1,True))
  for _ in range(2):self.assertEqual(self.client.put(url,headers=self.headers,json={'liked':False}).json()['likes'],0)

 def test_legacy_editorial_likes_add_to_existing_count(self):
  from community_store import CommunityStore
  p=CommunityStore(m.settings).create({'title':'策展作品','result_url':'https://fixture.invalid/a.jpg','status':'published','likes':5})
  r=self.client.put('/api/community/posts/'+p['id']+'/like',headers=self.headers,json={'liked':True})
  self.assertEqual(r.json()['likes'],6);self.assertEqual(self.feed(self.headers)[0]['likes'],6)

 def test_liking_hidden_or_pending_post_is_not_allowed(self):
  p=self.submit();url='/api/community/posts/'+p['id']+'/like'
  self.assertEqual(self.client.put(url,headers=self.headers,json={'liked':True}).status_code,404)
  self.review(p);self.withdraw(p);self.assertEqual(self.client.put(url,headers=self.headers,json={'liked':True}).status_code,404)

 def test_banned_user_cannot_submit_like_or_receive_feature_reward(self):
  p=self.submit();self.review(p);m.users.set_banned('sample_user',True)
  self.assertEqual(self.post().status_code,403)
  self.assertEqual(self.client.put('/api/community/posts/'+p['id']+'/like',headers=self.headers,json={'liked':True}).status_code,403)
  self.assertEqual(self.review(p,'feature').status_code,409);self.assertEqual(self.withdraw(p).status_code,200)

 def test_pending_quota_and_submission_rate_are_bounded(self):
  for i in range(5):jid=f'{i:012x}';self.make_job(jid);self.submit(job_id=jid)
  self.assertEqual(self.post().status_code,409);self.assertEqual(m.users.get_balance('sample_user'),100)

 def test_pending_expiry_and_published_cleanup_recovery(self):
  p=self.submit();self.store.users._conn.execute('UPDATE community_submissions SET submitted=? WHERE id=?',(time.time()-PENDING_TTL-1,p['id']));self.store.users._conn.commit()
  self.assertEqual(self.review(p).status_code,409);maintain(m);self.assertEqual(self.store.get(p['id'])['status'],'rejected')
  p=self.submit();self.review(p);key=self.store.get(p['id'])['payload']['result_key'];m.cleanup.schedule('cos',key,time.time()+100)
  maintain(m);self.assertIsNone(m.cleanup._conn.execute('SELECT due FROM cleanup WHERE target=?',(key,)).fetchone())

 def test_reject_reason_and_owner_reward_state_are_visible(self):
  p=self.submit();self.review(p,'reject',reason='请调整标题')
  row=self.client.get('/api/community/submissions/mine',headers=self.headers).json()['items'][0];self.assertEqual(row['reason'],'请调整标题');self.assertEqual(row['result_url'],'')

 def test_consented_comparison_is_copied_instead_of_hidden_full_original(self):
  key='norms/private/comparison.jpg';self.media[key]=800;m.jobs.update(self.job_id,comparison_cos=key)
  self.submit(share_original=True);self.assertEqual(self.copy_calls[1][0],key)

 def test_zero_reward_is_recorded_once_even_after_configuration_change(self):
  p=self.submit();self.review(p);m.settings.update({'rewards':{'community_featured':0}})
  r=self.review(p,'feature').json()['submission'];self.assertTrue(r['rewarded']);self.assertEqual(r['reward'],0)
  self.review(p,'unfeature');m.settings.update({'rewards':{'community_featured':99}});self.review(p,'feature')
  self.assertEqual(m.users.get_balance('sample_user'),100)

 def test_withdraw_cleanup_guards_against_late_copy_completion(self):
  p=self.submit();key=self.store.get(p['id'])['payload']['result_key'];self.withdraw(p)
  queued=m.cleanup._conn.execute('SELECT due FROM cleanup WHERE target=?',(key,)).fetchone()
  self.assertIsNotNone(queued);self.assertGreater(queued[0],time.time())
  self.media[key]=1000
  with patch('cleanup_store.time.time',return_value=queued[0]+1):m.cleanup.run(m.settings)
  self.assertNotIn(key,self.media)

 def test_legacy_id_with_submission_prefix_remains_likeable(self):
  m.settings.update({'community':{'enabled':True,'items':[{'id':'s_legacy','title':'旧展品','result_url':'https://fixture.invalid/a.jpg'}]}})
  r=self.client.put('/api/community/posts/s_legacy/like',headers=self.headers,json={'liked':True})
  self.assertEqual(r.status_code,200,r.text);self.assertEqual(r.json()['likes'],1)

 def test_public_default_preserves_legacy_capacity_and_supports_pages(self):
  items=[{'id':'legacy'+str(i),'title':'展品'+str(i),'result_url':'https://fixture.invalid/a.jpg'} for i in range(101)]
  m.settings.update({'community':{'enabled':True,'items':items}})
  whole=self.client.get('/api/community').json();self.assertEqual(len(whole['items']),101);self.assertFalse(whole['has_more'])
  first=self.client.get('/api/community?limit=20').json();second=self.client.get('/api/community?limit=20&offset=20').json()
  self.assertTrue(first['has_more']);self.assertEqual(first['next_offset'],20)
  self.assertEqual(len({p['id'] for p in first['items']+second['items']}),40)

if __name__=='__main__':
 names=[n for n in SubmissionTests.__dict__ if n.startswith('test_')]
 result=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(SubmissionTests(n) for n in names));failed={t._testMethodName for t,_ in result.failures+result.errors}
 (OUTPUT/'community_submissions_results.json').write_text(json.dumps({'cases':[{'case':n,'passed':n not in failed} for n in names]},indent=2),encoding='utf-8')
 for c in initial_connections:
  try:c.close()
  except Exception:pass
 print('COMMUNITY_SUBMISSIONS_SUMMARY total=%d passed=%d failed=%d'%(result.testsRun,result.testsRun-len(failed),len(failed)))
 raise SystemExit(0 if result.wasSuccessful() else 1)
