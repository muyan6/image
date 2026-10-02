"""Registered website community and manual-credit integration, isolated fixtures."""
import json,unittest
from unittest.mock import patch
from test_community_interactions import InteractionTests,m,OUTPUT,initial_connections
from account_links import register_verified,bind_verified
from tencent_cs import ModerationError
import community_api


class SiteCommunityTests(InteractionTests):
    def setUp(self):
        super().setUp()
        value=self.client.post('/api/auth/site/register',headers={'X-Site-Request':'1','Origin':'https://image.myil.top'},json={'username':'website_reader','password':'fixture-password-2026'})
        self.assertEqual(value.status_code,200,value.text)
        self.site=value.json();self.site_headers={'Authorization':'Bearer '+self.site['token'],'X-Site-Request':'1','Origin':'https://image.myil.top'}
        self.web=m._current_user(__import__('starlette.requests',fromlist=['Request']).Request({'type':'http','method':'GET','headers':[(b'authorization',('Bearer '+self.site['token']).encode())]}))['auth_identity']
        m.settings.update({'tencent':{'secret_id':'fixture-id','secret_key':'fixture-secret'}})
    def test_registered_zero_balance_comment_uses_tencent_not_fake_wechat(self):
        before=m.users.get_balance(self.web)
        with patch.object(community_api,'moderate_text',return_value=('Pass','Normal',0)) as moderation:
            value=self.comment('website_comment',headers=self.site_headers)
        self.assertEqual(value.status_code,200,value.text);moderation.assert_called_once();self.check.assert_not_called()
        self.assertEqual(m.users.get_balance(self.web),before)
        comments=self.list_comments(headers=self.site_headers);self.assertTrue(comments['items'][0]['mine'])
    def test_website_review_risk_and_service_failure_never_publish(self):
        for suggestion in ('Review','Block'):
            with patch.object(community_api,'moderate_text',return_value=(suggestion,'Risk',100)):
                self.assertEqual(self.comment('website_'+suggestion,headers=self.site_headers).status_code,400)
        with patch.object(community_api,'moderate_text',side_effect=ModerationError('fixture outage')):
            self.assertEqual(self.comment('website_error',headers=self.site_headers).status_code,503)
        self.assertEqual(self.list_comments()['total'],0)
    def test_old_web_comment_remains_owned_after_verified_link(self):
        with patch.object(community_api,'moderate_text',return_value=('Pass','Normal',0)):
            value=self.comment('before_binding',headers=self.site_headers)
        self.assertEqual(value.status_code,200,value.text);cid=value.json()['id']
        register_verified(m.users,'fixture-app','sample_user','same_union','mini')
        bind_verified(m.users,self.web,'sample_user','same_union','official-app')
        self.assertTrue(self.list_comments(headers=self.site_headers)['items'][0]['mine'])
        self.assertEqual(self.client.delete('/api/community/comments/'+cid,headers=self.site_headers).status_code,200)
        self.assertEqual(self.list_comments()['total'],0)
    def test_admin_filters_legacy_visitors_before_limit_and_manual_delta_is_visible(self):
        for i in range(35):m.users.ensure_user('web-legacy-'+str(i),account_type='web',welcome_balance=123)
        data=self.admin.get('/admin/api/users?account_type=web&limit=1').json()
        self.assertEqual(len(data['items']),1);self.assertEqual(data['items'][0]['username'],'website_reader')
        before=m.users.get_balance(self.web)
        result=self.admin.put('/admin/api/users/'+self.web+'/balance',json={'balance':17})
        self.assertEqual(result.status_code,200,result.text)
        items=self.client.get('/api/me/credits',headers=self.site_headers).json()['items']
        self.assertEqual(items[0]['amount'],17-before);self.assertEqual(items[0]['title'],'光子调整')
        invalid=self.admin.put('/admin/api/users/'+self.web+'/balance',json={'delta':True})
        self.assertEqual(invalid.status_code,400);self.assertEqual(m.users.get_balance(self.web),17)
    def test_admin_password_reset_revokes_site_but_not_mini_account(self):
        result=self.admin.post('/admin/api/users/'+self.web+'/site-password',json={'new_password':'new-fixture-password-2026'})
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(self.client.get('/api/me',headers=self.site_headers).status_code,401)
        self.assertEqual(self.client.get('/api/me',headers=self.headers).status_code,200)


if __name__=='__main__':
    # Inherited mini-program interaction cases are already executed by their suite.
    names=[name for name in SiteCommunityTests.__dict__ if name.startswith('test_')]
    suite=unittest.TestSuite(SiteCommunityTests(name) for name in names)
    result=unittest.TextTestRunner(verbosity=2).run(suite);failed={t._testMethodName for t,_ in result.failures+result.errors}
    (OUTPUT/'site_community_sync_results.json').write_text(json.dumps({'cases':[{'case':name,'passed':name not in failed} for name in names]},indent=2),encoding='utf-8')
    for conn in initial_connections:
        try:conn.close()
        except Exception:pass
    print('SITE_COMMUNITY_SYNC_SUMMARY total=%d passed=%d failed=%d'%(result.testsRun,result.testsRun-len(failed),len(failed)))
    raise SystemExit(0 if result.wasSuccessful() else 1)
