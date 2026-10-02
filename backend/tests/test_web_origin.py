"""Origin checks behind an HTTP upstream; no live WeChat or cloud calls."""
import json,os,unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from test_platform import PlatformTests,m,web_login,OUTPUT,initial_connections


class HttpUpstream:
    def __init__(self,app):self.app=app
    async def __call__(self,scope,receive,send):
        if scope['type']=='http':
            scope={**scope,'scheme':'http','server':('127.0.0.1',8000),
                   'headers':[(k,v) for k,v in scope['headers'] if k.lower()!=b'host']+[(b'host',b'127.0.0.1:8000')]}
        await self.app(scope,receive,send)


class OriginTests(PlatformTests):
    def setUp(self):
        super().setUp()
        self.env=patch.dict(os.environ,{'WEB_PUBLIC_ORIGIN':'https://image.myil.top'});self.env.start()

    def tearDown(self):
        self.env.stop();super().tearDown()

    def test_https_origin_http_upstream_start_and_poll(self):
        client=TestClient(HttpUpstream(m.app),base_url='https://image.myil.top')
        try:
            headers=self.browser_headers({'Origin':'https://image.myil.top'})
            r=client.post('/api/auth/wechat-web/start',headers=headers)
            self.assertEqual(r.status_code,200,r.text)
            self.assertIn('Secure',r.headers['set-cookie']);self.assertIn('HttpOnly',r.headers['set-cookie'])
            sid=r.json()['id']
            self.assertEqual(client.get('/api/auth/wechat-web/'+sid+'/status',headers=self.browser_headers()).json()['state'],'pending')
            self.assertEqual(self.approve(sid).status_code,200)
            result=client.get('/api/auth/wechat-web/'+sid+'/status',headers=headers)
            self.assertEqual(result.status_code,200,result.text)
            self.assertIsNone(m.verify_user_token(result.json()['token']))
            from web_accounts import store_for
            self.assertEqual(store_for(m.users).session(result.json()['token'])['account_id'],self.site_identity)
        finally:client.close()

    def test_public_origin_accepts_case_and_default_port(self):
        r=self.client.post('/api/auth/wechat-web/start',headers=self.browser_headers({'Origin':'https://IMAGE.MYIL.TOP:443'}))
        self.assertEqual(r.status_code,200,r.text);self.assertIn('Secure',r.headers['set-cookie'])

    def test_other_domains_ports_downgrade_and_malformed_rejected(self):
        for origin in ['https://evil.invalid','https://image.myil.top.evil.invalid','http://image.myil.top',
                       'https://image.myil.top:444','https://image.myil.top:0','null',
                       'https://image.myil.top@evil.invalid','https://image.myil.top/path',
                       'https://image.myil.top?query','https://image.myil.top#fragment']:
            r=self.client.post('/api/auth/wechat-web/start',headers={'X-Web-Login':'1','Origin':origin})
            self.assertEqual(r.status_code,403,origin)

    def test_forged_forwarded_and_host_headers_do_not_allow_attacker(self):
        r=self.client.post('/api/auth/wechat-web/start',headers={'X-Web-Login':'1','Origin':'https://evil.invalid',
                           'Host':'evil.invalid','X-Forwarded-Host':'evil.invalid','X-Forwarded-Proto':'https'})
        self.assertEqual(r.status_code,403)
        r=self.client.post('/api/auth/wechat-web/start',headers={'X-Web-Login':'1','Origin':'https://image.myil.top','Sec-Fetch-Site':'cross-site'})
        self.assertEqual(r.status_code,403)

    def test_explicit_custom_public_origin(self):
        with patch.dict(os.environ,{'WEB_PUBLIC_ORIGIN':'https://photos.example:8443/'}):
            r=self.client.post('/api/auth/wechat-web/start',headers=self.browser_headers({'Origin':'https://photos.example:8443'}))
            self.assertEqual(r.status_code,200)
            r=self.client.post('/api/auth/wechat-web/start',headers={'X-Web-Login':'1','Origin':'https://image.myil.top'})
            self.assertEqual(r.status_code,403)

    def test_invalid_configuration_fails_closed(self):
        with patch.dict(os.environ,{'WEB_PUBLIC_ORIGIN':'*'}):
            r=self.client.post('/api/auth/wechat-web/start',headers={'X-Web-Login':'1','Origin':'https://evil.invalid'})
            self.assertEqual(r.status_code,503)

    def test_direct_loopback_development_and_required_custom_header(self):
        client=TestClient(m.app,base_url='http://127.0.0.1:8000')
        try:
            r=client.post('/api/auth/wechat-web/start',headers=self.browser_headers({'Origin':'http://127.0.0.1:8000'}))
            self.assertEqual(r.status_code,200);self.assertNotIn('Secure',r.headers['set-cookie'])
            self.assertEqual(client.post('/api/auth/wechat-web/start',headers={'Origin':'http://127.0.0.1:8000'}).status_code,403)
        finally:client.close()


if __name__=='__main__':
    suite=unittest.TestSuite(OriginTests(n) for n in OriginTests.__dict__ if n.startswith('test_'))
    names=[t._testMethodName for t in suite];r=unittest.TextTestRunner(verbosity=2).run(suite)
    failed={t._testMethodName for t,_ in r.failures+r.errors}
    (OUTPUT/'web_origin_results.json').write_text(json.dumps({'cases':[{'case':n,'passed':n not in failed} for n in names]},indent=2),encoding='utf-8')
    for c in initial_connections:
        try:c.close()
        except Exception:pass
    print('WEB_ORIGIN_SUMMARY total=%d passed=%d failed=%d'%(r.testsRun,r.testsRun-len(failed),len(failed)))
    raise SystemExit(0 if r.wasSuccessful() else 1)
