"""Offline interaction, moderation, mixed admin statistics and invitation regressions."""
import json,sqlite3,time,unittest
from unittest.mock import Mock
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from test_community_submissions import SubmissionTests,m,OUTPUT,initial_connections
from community_interactions import interactions_for
from user_store import UserStore
import wechat_sec


class InteractionTests(SubmissionTests):
 def setUp(self):
  super().setUp()
  m.settings.update({'wechat':{'app_id':'fixture-app','app_secret':'fixture-secret'},'moderation':{'enabled':True}})
  self.p=self.submit();self.assertEqual(self.review(self.p).status_code,200)
  self.url='/api/community/posts/'+self.p['id']
  self.wx=patch.object(wechat_sec,'check_text',return_value=('pass','100',0));self.check=self.wx.start();self.extra.append(self.wx)
 def comment(self,request_id='request_12345',headers=None,**kw):
  return self.client.post(self.url+'/comments',headers=headers or self.headers,json={'content':'喜欢这张作品的色彩','request_id':request_id,**kw})
 def list_comments(self,headers=None):return self.client.get(self.url+'/comments',headers=headers or self.headers).json()
 def cid(self,**kw):
  r=self.comment(**kw);self.assertEqual(r.status_code,200,r.text);return r.json()['id']
 def test_admin_counts_include_published_submissions_and_search(self):
  d=self.admin.get('/admin/api/community/posts').json();self.assertEqual((d['total'],d['stats']['published']), (1,1))
  self.assertEqual(d['items'][0]['source'],'submission')
  self.assertEqual(self.admin.get('/admin/api/community/posts?q=真实作者').json()['total'],1)
  self.assertEqual(self.admin.get('/admin/api/community/posts?status=paused').json()['total'],0)
  r=self.admin.post('/admin/api/community/posts',json={'title':'手工帖子','result_url':'https://fixture.invalid/a.jpg','status':'published'})
  self.assertEqual(r.status_code,200,r.text)
  d=self.admin.get('/admin/api/community/posts?limit=1&offset=1').json();self.assertEqual(d['total'],2);self.assertEqual(d['stats']['total'],2);self.assertEqual(len(d['items']),1)
 def test_public_detail_and_owner_original_consent_match_feed(self):
  d=self.client.get(self.url).json()['post'];self.assertEqual(d['id'],self.p['id']);self.assertEqual(d['origUrl'],'');self.assertNotIn('owner',d)
  self.assertEqual(self.client.get('/api/community/posts/unknown').status_code,404)
  self.assertEqual(self.client.get(self.url+'/comments').json()['total'],0)
 def test_pass_comments_publish_with_wechat_scene_two_only(self):
  cid=self.cid();self.check.assert_called_once_with(m.settings,'喜欢这张作品的色彩','sample_user',scene=2)
  d=self.list_comments();self.assertEqual(d['total'],1);self.assertTrue(d['items'][0]['mine']);self.assertNotIn('owner',d['items'][0])
  self.assertEqual(self.client.get('/api/community').json()['items'][0]['comments'],1)
  self.assertEqual(self.client.get(self.url).json()['post']['comments'],1)
  self.assertEqual(interactions_for(m.users).get(cid)['verdict'],'wechat:pass:100')
 def test_risk_review_and_errors_are_never_published(self):
  for index,suggestion in enumerate(('risk','review')):
   self.check.return_value=(suggestion,'20002',0);self.assertEqual(self.comment('reject_test_'+str(index)).status_code,400)
  self.check.side_effect=wechat_sec.WechatSecError('network',code='NETWORK');self.assertEqual(self.comment('error_test_1').status_code,503)
  self.assertEqual(self.list_comments()['total'],0)
  self.assertFalse(m.users._conn.execute('SELECT 1 FROM community_comments WHERE content<>\'\'').fetchone())
 def test_disabled_missing_and_banned_users_fail_before_wechat(self):
  m.settings.update({'moderation':{'enabled':False}});self.assertEqual(self.comment().status_code,503)
  m.settings.update({'moderation':{'enabled':True},'wechat':{'app_secret':''}});self.assertEqual(self.comment().status_code,503)
  self.check.assert_not_called()
  m.users.set_banned('sample_user',True);self.assertEqual(self.comment().status_code,403)
  self.assertEqual(self.client.post(self.url+'/comments',json={'content':'a','request_id':'abcdefgh'}).status_code,401)
 def test_idempotent_retry_and_changed_payload_are_checked(self):
  cid=self.cid();self.assertEqual(self.comment().json()['id'],cid);self.assertEqual(self.check.call_count,1)
  self.assertEqual(self.comment(content='不同内容').status_code,429);self.assertEqual(self.list_comments()['total'],1)
 def test_failed_moderation_can_retry_without_duplicate_comment(self):
  self.check.side_effect=wechat_sec.WechatSecError('timeout');self.assertEqual(self.comment().status_code,503)
  self.check.side_effect=None;cid=self.cid();self.assertEqual(self.list_comments()['total'],1)
  self.assertEqual(m.users._conn.execute('SELECT COUNT(*) FROM community_comments').fetchone()[0],1)
 def test_comment_like_is_idempotent_and_persists(self):
  cid=self.cid();url='/api/community/comments/'+cid+'/like'
  for _ in range(2):self.assertEqual(self.client.put(url,headers=self.headers,json={'liked':True}).json()['likes'],1)
  self.assertTrue(self.list_comments()['items'][0]['liked'])
  self.assertEqual(self.client.put(url,headers=self.headers,json={'liked':False}).json()['likes'],0)
  self.assertEqual(self.client.put(url,headers=self.headers,json={'liked':'yes'}).status_code,422)
 def test_own_delete_foreign_forbidden_and_admin_can_delete_all(self):
  cid=self.cid();url='/api/community/comments/'+cid
  other={'Authorization':'Bearer '+m.user_token('other_user')}
  self.assertEqual(self.client.delete(url,headers=other).status_code,404)
  self.assertEqual(self.admin.delete('/admin/api/community/comments/'+cid).status_code,200)
  self.assertEqual(self.list_comments()['total'],0)
  self.assertEqual(self.client.put(url+'/like',headers=self.headers,json={'liked':True}).status_code,404)
  cid=self.cid(request_id='own_delete_123');self.assertEqual(self.client.delete('/api/community/comments/'+cid,headers=self.headers).status_code,200)
 def test_report_both_kinds_duplicate_snapshot_and_resolution(self):
  cid=self.cid()
  for url in (self.url+'/report','/api/community/comments/'+cid+'/report'):
   r=self.client.post(url,headers=self.headers,json={'reason':'spam'});self.assertEqual(r.status_code,200,r.text)
   self.assertTrue(self.client.post(url,headers=self.headers,json={'reason':'spam'}).json()['duplicate'])
  d=self.admin.get('/admin/api/community/reports').json();self.assertEqual(d['total'],2)
  self.assertTrue(any(p['snapshot']=='喜欢这张作品的色彩' for p in d['items']))
  self.assertEqual(self.client.post(self.url+'/report',headers=self.headers,json={'reason':'invalid'}).status_code,400)
  self.admin.post('/admin/api/community/reports/'+d['items'][0]['id']+'/resolve')
  self.assertEqual(self.admin.get('/admin/api/community/reports').json()['total'],1)
  self.assertEqual(self.list_comments()['total'],1) # Reports never arbitrarily auto-delete.
 def test_admin_routes_are_guarded(self):
  for path in ('/admin/api/community/comments','/admin/api/community/reports'):
   self.assertEqual(self.client.get(path).status_code,403)
  cid=self.cid();self.assertEqual(self.client.delete('/admin/api/community/comments/'+cid).status_code,403)
 def test_withdraw_during_wechat_never_publishes(self):
  def withdraw(*a,**kw):self.withdraw(self.p);return ('pass','100',0)
  self.check.side_effect=withdraw;self.assertEqual(self.comment().status_code,404)
  self.assertEqual(interactions_for(m.users).comments(admin=True)['total'],0)
 def test_deleted_during_wechat_cannot_resurrect(self):
  def delete(*a,**kw):
   cid=m.users._conn.execute('SELECT id FROM community_comments').fetchone()[0]
   interactions_for(m.users).delete_comment(cid);return ('pass','100',0)
  self.check.side_effect=delete;self.assertEqual(self.comment().status_code,409)
  self.assertEqual(self.list_comments()['total'],0)
 def test_concurrent_retry_runs_one_moderation_call(self):
  import threading
  entered=threading.Event();release=threading.Event()
  def waiting(*a,**kw):entered.set();release.wait(5);return ('pass','100',0)
  self.check.side_effect=waiting
  with ThreadPoolExecutor(max_workers=2) as ex:
   future=ex.submit(self.comment);self.assertTrue(entered.wait(5))
   try:self.assertEqual(self.comment().status_code,429)
   finally:release.set()
   self.assertEqual(future.result().status_code,200)
  self.assertEqual(self.check.call_count,1)
 def test_comment_rate_limit_before_external_request(self):
  for n in range(5):self.cid(request_id='bounded_comment_'+str(n))
  self.assertEqual(self.comment('bounded_comment_6').status_code,429);self.assertEqual(self.check.call_count,5)
 def test_hidden_posts_and_banned_comment_authors_are_invisible(self):
  other={'Authorization':'Bearer '+m.user_token('other_user')};cid=self.cid(headers=other)
  m.users.set_banned('other_user',True);self.assertEqual(self.list_comments()['total'],0)
  self.assertEqual(self.client.put('/api/community/comments/'+cid+'/like',headers=self.headers,json={'liked':True}).status_code,404)
  self.withdraw(self.p)
  self.assertEqual(self.client.get(self.url).status_code,404);self.assertEqual(self.client.get(self.url+'/comments').status_code,404)
 def test_admin_delete_submission_hides_post_and_cleans_copy(self):
  self.cid();self.assertEqual(self.admin.delete('/admin/api/community/posts/'+self.p['id']).status_code,200)
  self.assertEqual(self.client.get(self.url).status_code,404);self.assertEqual(self.admin.get('/admin/api/community/posts').json()['stats']['total'],0)
  self.assertNotIn(self.store.get(self.p['id'])['payload']['result_key'],self.media)
 def test_comment_pagination_has_no_duplicate_rows(self):
  store=interactions_for(m.users)
  for n in range(4):self.cid(request_id='page_comment_'+str(n))
  a=self.client.get(self.url+'/comments?limit=2').json();b=self.client.get(self.url+'/comments?limit=2&offset=2').json()
  self.assertEqual(a['total'],4);self.assertTrue(a['has_more']);self.assertFalse(b['has_more'])
  self.assertEqual(len({p['id'] for p in a['items']+b['items']}),4)
 def test_invite_details_are_owner_scoped_and_keep_actual_reward(self):
  code=m.users.get_user('sample_user')['invite_code'];m.users.bind_invite_once('other_user',code,45)
  m.settings.update({'rewards':{'invite':99}})
  d=self.client.get('/api/me/invites',headers=self.headers).json()
  self.assertEqual((d['total'],d['recorded_reward']),(1,45));self.assertEqual(d['items'][0]['reward'],45)
  self.assertNotIn('other_user',json.dumps(d));self.assertNotIn('openid',d['items'][0])
  other={'Authorization':'Bearer '+m.user_token('other_user')};self.assertEqual(self.client.get('/api/me/invites',headers=other).json()['total'],0)
  self.assertEqual(self.client.get('/api/me/invites').status_code,401)
 def test_invite_legacy_migration_preserves_balance_and_unknown_amount(self):
  folder=self.d/'legacy-invites';folder.mkdir();db=sqlite3.connect(str(folder/'users.db'))
  db.execute('CREATE TABLE invite_bindings(invitee TEXT PRIMARY KEY,inviter TEXT NOT NULL,created_at REAL NOT NULL)')
  db.execute('INSERT INTO invite_bindings VALUES(?,?,?)',('old-friend','old-owner',time.time()));db.commit();db.close()
  users=UserStore(str(folder))
  try:
   users.ensure_user('old-owner');users.set_balance('old-owner',277)
   d=users.invite_records('old-owner');self.assertEqual(d['items'][0]['reward'],None);self.assertEqual(d['historical_unknown'],1)
   self.assertEqual(users.get_balance('old-owner'),277)
  finally:users.close()

 def test_wechat_client_scene_and_malformed_responses_fail_closed(self):
  self.wx.stop()
  try:
   response=Mock();response.json.return_value={'errcode':0,'result':{'suggest':'pass','label':100}}
   with patch.object(wechat_sec,'get_access_token',return_value='fixture-token'),patch.object(wechat_sec.requests,'post',return_value=response) as send:
    self.assertEqual(wechat_sec.check_text(m.settings,'留言','sample_user',scene=2),('pass','100',0))
    self.assertEqual(send.call_args.kwargs['json']['scene'],2);response.raise_for_status.assert_called_once()
    for data in ([],None,{'result':[]},{'result':{'suggest':'unknown'}}):
     response.json.return_value=data
     with self.assertRaises(wechat_sec.WechatSecError):wechat_sec.check_text(m.settings,'留言','sample_user',scene=2)
  finally:self.wx.start()


if __name__=='__main__':
 names=[n for n in InteractionTests.__dict__ if n.startswith('test_')]
 result=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(InteractionTests(n) for n in names));failed={t._testMethodName for t,_ in result.failures+result.errors}
 (OUTPUT/'community_interactions_results.json').write_text(json.dumps({'cases':[{'case':n,'passed':n not in failed} for n in names]},indent=2),encoding='utf-8')
 for c in initial_connections:
  try:c.close()
  except Exception:pass
 print('COMMUNITY_INTERACTIONS_SUMMARY total=%d passed=%d failed=%d'%(result.testsRun,result.testsRun-len(failed),len(failed)))
 raise SystemExit(0 if result.wasSuccessful() else 1)
