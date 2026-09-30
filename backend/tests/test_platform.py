"""Offline WeChat-only web auth, retention, deletion and scheduler regressions."""
import json,time,unittest
from pathlib import Path
from unittest.mock import patch,Mock
from fastapi.testclient import TestClient
from test_cloud_pipeline import CloudTests,m,cp,OUTPUT,initial_connections
import web_login


class PlatformTests(CloudTests):
    def setUp(self):
        super().setUp()
        m.settings.update({'wechat':{'app_id':'fixture-app','app_secret':'fixture-secret'}})

    def start(self,client=None):
        r=(client or self.client).post('/api/auth/wechat-web/start',headers={'X-Web-Login':'1'})
        self.assertEqual(r.status_code,200,r.text);return r.json()

    def tearDown(self):
        m.web_login_router.close()
        super().tearDown()

    def approve(self,sid,action='approve',headers=None):
        return self.client.post('/api/auth/wechat-web/approve',headers=headers or self.headers,json={'id':sid,'action':action})

    def status(self,sid,client=None):
        return (client or self.client).get('/api/auth/wechat-web/'+sid+'/status',headers={'X-Web-Login':'1'})

    def test_web_guest_disabled_and_wechat_balance_shared(self):
        self.assertEqual(self.client.post('/api/auth/web').status_code,401)
        m.users.ensure_user('web-fixture');m.users.set_balance('web-fixture',245)
        self.assertEqual(self.client.get('/api/me',headers={'Authorization':'Bearer '+m.user_token('web-fixture')}).status_code,401)
        self.assertEqual(m.users.get_balance('web-fixture'),245)
        d=self.start();self.assertEqual(self.status(d['id']).json()['state'],'pending')
        self.assertEqual(self.approve(d['id']).status_code,200)
        r=self.status(d['id']);self.assertEqual(r.status_code,200)
        token=r.json()['token'];self.assertEqual(m.verify_user_token(token),'sample_user')
        m.users.set_balance('sample_user',177)
        self.assertEqual(self.client.get('/api/me',headers={'Authorization':'Bearer '+token}).json()['balance'],177)
        self.assertEqual(self.status(d['id']).json()['balance'],177)

    def test_another_browser_cannot_consume_login(self):
        d=self.start();self.approve(d['id'])
        with TestClient(m.app) as other:self.assertEqual(self.status(d['id'],other).status_code,403)
        self.assertEqual(self.approve(d['id']).status_code,409)

    def test_cancel_expiry_and_config_change(self):
        d=self.start();self.approve(d['id'],'deny')
        self.assertEqual(self.status(d['id']).json()['state'],'denied')
        d=self.start()
        secret=self.client.cookies.get(web_login.COOKIE)
        with patch.object(web_login.time,'time',return_value=time.time()+301):
            r=self.client.get('/api/auth/wechat-web/'+d['id']+'/status',headers={'X-Web-Login':'1','Cookie':web_login.COOKIE+'='+secret})
            self.assertEqual(r.status_code,410)
        m.settings.update({'wechat':{'app_id':'another-app'}})
        self.assertEqual(self.status(d['id']).status_code,410)

    def test_origin_guard_and_rate_limit(self):
        self.assertEqual(self.client.post('/api/auth/wechat-web/start').status_code,403)
        self.assertEqual(self.client.post('/api/auth/wechat-web/start',headers={'X-Web-Login':'1','Origin':'https://evil.invalid'}).status_code,403)
        for _ in range(5):self.start()
        self.assertEqual(self.client.post('/api/auth/wechat-web/start',headers={'X-Web-Login':'1'}).status_code,429)

    def test_qr_cached_and_scene_points_to_confirmation_page(self):
        d=self.start();r=Mock();r.content=b'\x89PNG\r\n\x1a\nfixture';r.raise_for_status.return_value=None
        with patch.object(web_login.wechat_sec,'get_access_token',return_value='fixture-token'),patch.object(web_login.requests,'post',return_value=r) as submit:
            self.assertEqual(self.client.get(d['qr_url']).status_code,200)
            self.assertEqual(self.client.get(d['qr_url']).status_code,200)
        submit.assert_called_once();self.assertEqual(submit.call_args.kwargs['json']['scene'],d['id'])
        self.assertEqual(submit.call_args.kwargs['json']['page'],'pages/web-login/web-login')
        self.assertEqual(len(d['id']),32)

    def test_approval_requires_authentication_and_does_not_create_users(self):
        d=self.start()
        self.assertEqual(self.client.post('/api/auth/wechat-web/approve',json={'id':d['id']}).status_code,401)
        self.assertEqual(self.approve(d['id'],'arbitrary').status_code,400)
        self.assertEqual(m.users._conn.execute('SELECT COUNT(*) FROM users').fetchone()[0],2)

    def test_original_and_result_30_days_from_completion(self):
        jid=self.job(age=29*86400,original_age=30*86400)
        job=m.jobs.get(jid)
        self.assertEqual(m.ORIGINAL_TTL_SECONDS,30*86400)
        self.assertTrue(m._job_media_url(job,'orig'));self.assertTrue(m._job_media_url(job,'result'))
        self.assertEqual(m._media_expires_at(job,'orig'),m._media_expires_at(job,'result'))
        m.jobs.update(jid,completed_at=time.time()-31*86400)
        self.assertIsNone(m._job_media_url(m.jobs.get(jid),'result'))

    def test_retention_migrates_old_24h_cleanup_without_reviving_deleted(self):
        jid=self.job();m.jobs.update(jid,orig_cos='origins/fixture.jpg',result_cos='results/fixture.jpg')
        m.cleanup.schedule('cos','origins/fixture.jpg',time.time()+100)
        m._startup_file_gc()
        due=m.cleanup._conn.execute("SELECT due FROM cleanup WHERE target='origins/fixture.jpg'").fetchone()[0]
        self.assertGreater(due,time.time()+29*86400)
        m.jobs.delete_for_openid(jid,'sample_user');m._startup_file_gc()
        due=m.cleanup._conn.execute("SELECT due FROM cleanup WHERE target='origins/fixture.jpg'").fetchone()[0]
        self.assertLessEqual(due,time.time())
        m._retain_job_object(jid,'cos','origins/fixture.jpg',time.time()+30*86400)
        due=m.cleanup._conn.execute("SELECT due FROM cleanup WHERE target='origins/fixture.jpg'").fetchone()[0]
        self.assertLessEqual(due,time.time())

    def test_retained_original_is_not_deleted_by_older_file_mtime(self):
        import os
        jid=self.job(age=29*86400,original_age=31*86400)
        p=Path(m.UPLOAD_DIR)/m.jobs.get(jid)['orig_file'];old=time.time()-31*86400
        os.utime(p,(old,old));m._startup_file_gc()
        self.assertTrue(p.exists())

    def test_delete_cleans_owned_source_and_audit_copies_immediately(self):
        m.settings.update({'cloud_pipeline':{'audit_mode':'wechat_auto'},'moderation':{'enabled':True}})
        jid=self.new();self.step(jid,'prepare');row=self.cloud.audits.row(jid,'input')
        with patch.object(m.cleanup,'delete_cos_now',return_value=5) as delete:
            r=self.client.delete('/api/my/jobs/'+jid,headers=self.headers)
        self.assertEqual(r.status_code,200)
        keys=delete.call_args.args[1]
        self.assertIn(row['capability'],keys);self.assertIn(self.source['key'],keys)
        self.assertEqual(self.cloud.audits.row(jid,'input')['state'],'done')
        self.assertIsNone(m._job_media_url(m.jobs.get(jid),'result'))
        self.assertEqual(m.users.get_balance('sample_user'),100)

    def test_active_index_survives_update_deletion_restart(self):
        jid=self.new()
        with patch.object(m.jobs,'list_recent',side_effect=AssertionError('HISTORY_SCAN')):
            self.assertEqual([j['id'] for j in self.cloud.pending()],[jid])
        m.jobs.update(jid,status='succeeded')
        self.assertEqual(self.cloud.pending(),[])
        m.jobs.update(jid,status='processing')
        self.assertEqual(len(self.cloud.pending()),1)
        m.jobs.delete_for_openid(jid,'sample_user');self.assertEqual(self.cloud.pending(),[])

    def test_capacity_does_not_evict_unexpired_works(self):
        old=m.jobs._max;m.jobs._max=1
        try:
            first=self.job();self.new()
            self.assertIsNotNone(m.jobs.get(first))
        finally:m.jobs._max=old

    def test_admin_unban_can_reset_counter_without_erasing_history(self):
        for i in range(3):m.users.record_violation('sample_user','test-'+str(i),'image','fixture',0)
        self.assertTrue(m.users.get_user('sample_user')['banned'])
        r=self.admin.put('/admin/api/users/sample_user/ban',json={'banned':False,'reset_count':True})
        self.assertEqual(r.status_code,200,r.text)
        v=m.users.record_violation('sample_user','test-new','image','fixture',0)
        self.assertEqual(v['weekly_count'],1);self.assertFalse(v['banned'])
        self.assertEqual(len(m.users.list_violations()),4)

    def test_unban_without_reset_preserves_counter_and_invalid_type_rejected(self):
        for i in range(3):m.users.record_violation('sample_user','old-'+str(i),'image','fixture',0)
        m.users.set_banned('sample_user',False)
        self.assertTrue(m.users.record_violation('sample_user','new','image','fixture',0)['banned'])
        self.assertEqual(self.admin.put('/admin/api/users/sample_user/ban',json={'banned':'false'}).status_code,400)

    def test_default_latest_mode_and_legacy_admin_patch_normalized(self):
        r=self.admin.put('/admin/api/settings',json={'cloud_pipeline':{'enabled':False,'audit_mode':'ci_sync'}})
        self.assertEqual(r.status_code,200,r.text)
        self.assertTrue(m.settings.snapshot()['cloud_pipeline']['enabled'])
        self.assertEqual(m.settings.snapshot()['cloud_pipeline']['audit_mode'],'wechat_auto')

    def test_metadata_reuse_and_phase_timings(self):
        m.settings.update({'moderation':{'enabled':False}})
        jid=self.generating();job=self.completed(jid)
        self.assertEqual(job['status'],'succeeded')
        for key in ['prepare_ms','submit_ms','import_ms','finalize_ms','provider_ms']:self.assertIn(key,job['timings'])
        info_calls=[c.args[1] for c in cp.cos.image_info.call_args_list]
        self.assertEqual(info_calls.count(job['vendor_result_key']),1)


if __name__=='__main__':
    suite=unittest.TestSuite(PlatformTests(n) for n in PlatformTests.__dict__ if n.startswith('test_'))
    names=[t._testMethodName for t in suite];r=unittest.TextTestRunner(verbosity=2).run(suite)
    failed={t._testMethodName for t,_ in r.failures+r.errors}
    (OUTPUT/'platform_results.json').write_text(json.dumps({'cases':[{'case':n,'passed':n not in failed} for n in names]},indent=2),encoding='utf-8')
    if m.cloud._audits:m.cloud._audits.close();m.cloud._audits=None
    for c in initial_connections:
        try:c.close()
        except Exception:pass
    print('PLATFORM_SUMMARY total=%d passed=%d failed=%d'%(r.testsRun,r.testsRun-len(failed),len(failed)))
    raise SystemExit(0 if r.wasSuccessful() else 1)
