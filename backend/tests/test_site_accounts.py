"""Explicit site registration/session and prepared WeChat binding, offline only."""
import json
import re
import shutil
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from unittest.mock import patch
from fastapi.testclient import TestClient
from test_workflow import WorkflowTests, m, OUTPUT, ROOT, initial_connections
from web_accounts import store_for, password_matches, token_hash
from user_store import UserStore
from account_links import bind_verified, register_verified, canonical, aliases
import web_wechat
import wechat_auth


class SiteTests(WorkflowTests):
    def setUp(self):
        super().setUp()
        self.site_headers = {'X-Site-Request': '1', 'Origin': 'https://image.myil.top'}
        self.browser = TestClient(m.app, base_url='https://image.myil.top')

    def tearDown(self):
        self.browser.close(); super().tearDown()

    def register(self, username='fixture_user', password='fixture-password-01'):
        result = self.browser.post('/api/auth/site/register', headers=self.site_headers,
                                   json={'username': username, 'password': password})
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def account_id(self, username='fixture_user'):
        return m.users._conn.execute('SELECT account_id FROM web_credentials WHERE username=?', (username,)).fetchone()[0]

    def configured(self, kind='official'):
        m.settings.update({'wechat': {'app_id': 'mini_app', 'app_secret': 'fixture'},
            'web_wechat': {'enabled': True, 'kind': kind, 'app_id': 'site_app', 'app_secret': 'fixture-secret',
                          'public_origin': 'https://image.myil.top'}})

    def start_state(self):
        result = self.browser.post('/api/auth/site/wechat/start', headers=self.site_headers)
        self.assertEqual(result.status_code, 200, result.text)
        return parse_qs(urlsplit(result.json()['url']).query)['state'][0], result.json()

    def callback(self, state, data=None):
        data = data or {'openid': 'official_verified', 'unionid': 'verified_union', 'access_token': 'not-exposed-fixture'}
        response = type('FixtureResponse', (), {'status_code': 200, 'json': lambda self: data})()
        with patch.object(web_wechat.requests, 'get', return_value=response):
            return self.browser.get('/api/auth/site/wechat/callback?code=fixture-code&state=' + state,
                                    follow_redirects=False)

    def test_site_register_zero_even_configured_gift_and_salted_password(self):
        m.settings.update({'commerce': {'welcome_points': 9999}})
        result = self.register(); identity = self.account_id()
        self.assertEqual(result['balance'], 0); self.assertTrue(result['manual_credit_only'])
        self.assertTrue(result['account_user_id'].startswith('WEB-'))
        row = m.users._conn.execute('SELECT password_hash FROM web_credentials WHERE account_id=?', (identity,)).fetchone()
        self.assertNotIn('fixture-password', row[0]); self.assertTrue(row[0].startswith('pbkdf2_sha256$600000$'))
        self.assertTrue(password_matches('fixture-password-01', row[0]))
        self.assertFalse(password_matches('wrong-password', row[0]))
        self.assertEqual(m.users.get_balance(identity), 0)
        self.assertEqual(self.browser.post('/api/auth/site/register', headers=self.site_headers,
            json={'username': 'FIXTURE_USER', 'password': 'fixture-password-02'}).status_code, 400)
        self.assertEqual(m.users._conn.execute('SELECT COUNT(*) FROM web_credentials').fetchone()[0], 1)

    def test_site_guest_reads_never_register_and_legacy_visitor_token_rejected(self):
        before = m.users._conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]
        for path in ('/api/config', '/api/styles', '/api/community'):
            self.assertEqual(self.browser.get(path).status_code, 200)
        self.assertEqual(self.browser.get('/api/auth/site/session').status_code, 401)
        self.assertEqual(self.browser.get('/api/my/jobs').status_code, 401)
        self.assertEqual(m.users._conn.execute('SELECT COUNT(*) FROM users').fetchone()[0], before)
        m.users.ensure_user('web-111111111111')
        self.assertEqual(self.browser.get('/api/me', headers={'Authorization': 'Bearer ' + m.user_token('web-111111111111')}).status_code, 401)
        self.assertFalse(store_for(m.users).registered('web-111111111111'))
        self.assertEqual(self.browser.post('/api/auth/wechat-web/start', headers={'X-Web-Login': '1'}).status_code, 401)

    def test_site_secure_cookie_and_random_session_not_wechat_token(self):
        response = self.browser.post('/api/auth/site/register', headers=self.site_headers,
            json={'username': 'fixture_user', 'password': 'fixture-password-01'})
        cookie = response.headers['set-cookie']; data = response.json()
        self.assertIn('HttpOnly', cookie); self.assertIn('Secure', cookie); self.assertIn('SameSite=lax', cookie)
        self.assertIsNone(m.verify_user_token(data['token']))
        self.assertTrue(data['token'].startswith('site_'))
        self.assertIsNotNone(store_for(m.users).session(data['token']))
        self.assertEqual(self.browser.get('/api/me').json()['auth_source'], 'site')
        stored = m.users._conn.execute('SELECT token_hash FROM site_sessions').fetchone()[0]
        self.assertNotEqual(stored, data['token']); self.assertEqual(stored, token_hash(data['token']))

    def test_site_same_origin_custom_header_and_csrf_nonce_validation(self):
        data = self.register()
        self.assertEqual(self.browser.post('/api/me/profile', json={'nickname': 'fixture'}).status_code, 403)
        self.assertEqual(self.browser.post('/api/me/profile', headers={**self.site_headers, 'Origin': 'https://evil.invalid'},
                                          json={'nickname': 'fixture'}).status_code, 403)
        self.assertEqual(self.browser.post('/api/me/profile', headers={**self.site_headers, 'X-Site-CSRF': 'wrong'},
                                          json={'nickname': 'fixture'}).status_code, 403)
        for name in ('X-Site-CSRF', 'X-CSRF-Token'):
            response = self.browser.post('/api/me/profile', headers={**self.site_headers, name: data['csrf_token']}, json={'nickname': 'fixture'})
            self.assertEqual(response.status_code, 200, response.text)

    def test_site_login_logout_refresh_expiry_and_password_reset(self):
        original = self.register()
        refreshed = self.browser.post('/api/auth/site/session', headers=self.site_headers)
        self.assertEqual(refreshed.status_code, 200)
        self.assertIsNone(store_for(m.users).session(original['token']))
        self.assertNotEqual(refreshed.json()['token'], original['token'])
        self.assertEqual(self.browser.post('/api/auth/site/logout', headers=self.site_headers).status_code, 200)
        self.assertEqual(self.browser.get('/api/me').status_code, 401)
        self.assertEqual(self.browser.post('/api/auth/site/login', headers=self.site_headers,
            json={'username': 'fixture_user', 'password': 'wrong-password'}).status_code, 401)
        login = self.browser.post('/api/auth/site/login', headers=self.site_headers,
            json={'username': 'fixture_user', 'password': 'fixture-password-01'})
        self.assertEqual(login.status_code, 200)
        store_for(m.users).admin_reset(self.account_id(), 'new-fixture-password')
        self.assertEqual(self.browser.get('/api/me').status_code, 401)
        self.assertIsNone(store_for(m.users).session(login.json()['token']))
        self.assertEqual(self.browser.post('/api/auth/site/login', headers=self.site_headers,
            json={'username': 'fixture_user', 'password': 'new-fixture-password'}).status_code, 200)
        changed = self.browser.post('/api/auth/site/password', headers=self.site_headers,
            json={'current_password': 'new-fixture-password', 'new_password': 'changed-fixture-password'})
        self.assertEqual(changed.status_code, 200); self.assertTrue(changed.json()['login_required'])
        self.assertEqual(self.browser.get('/api/me').status_code, 401)

    def test_site_registration_and_login_ip_rate_limits_persist(self):
        store = store_for(m.users)
        for _ in range(5): store.rate_limit('fixture-ip', 'register')
        with self.assertRaises(m.HTTPException) as failure: store.rate_limit('fixture-ip', 'register')
        self.assertEqual(failure.exception.status_code, 429)
        for _ in range(30): store.rate_limit('login-ip', 'login', 'fixture')
        reopened = UserStore(str(self.d))
        try:
            with self.assertRaises(m.HTTPException): store_for(reopened).rate_limit('login-ip', 'login', 'fixture')
        finally: reopened.close()

    def test_site_concurrent_registration_creates_one_zero_balance_user(self):
        store = store_for(m.users)
        def register_one(index):
            try: return store.register('unique_fixture', 'fixture-password-01', 'ip_' + str(index))[0]
            except ValueError: return None
        with ThreadPoolExecutor(max_workers=3) as executor: identities = list(executor.map(register_one, range(3)))
        self.assertEqual(sum(identity is not None for identity in identities), 1)
        self.assertEqual(m.users._conn.execute("SELECT COUNT(*) FROM web_credentials WHERE username='unique_fixture'").fetchone()[0], 1)
        self.assertEqual(m.users.get_balance(next(identity for identity in identities if identity)), 0)

    def test_site_manual_adjustment_real_delta_audit_and_no_reward_payment(self):
        self.register(); identity = self.account_id()
        self.assertEqual(m.users.admin_adjust_balance(identity, balance=50), 50)
        self.assertEqual(m.users.admin_adjust_balance(identity, delta=-10), 40)
        self.assertEqual(self.browser.get('/api/me').json()['balance'], 40)
        amounts = [r['amount'] for r in self.browser.get('/api/me/credits').json()['items']]
        self.assertEqual(sorted(amounts), [-10, 50])
        for bad in (True, 1.5, '5'):
            with self.assertRaises(ValueError): m.users.admin_adjust_balance(identity, delta=bad)
        with self.assertRaises(ValueError):m.users.admin_adjust_balance(identity,balance=2**63)
        self.assertEqual(m.users.get_balance(identity),40)
        self.assertEqual(self.browser.post('/api/me/earn', headers=self.site_headers, json={'kind': 'checkin'}).status_code, 403)
        self.assertEqual(self.browser.post('/api/me/earn', headers=self.site_headers, json={'kind': 'video'}).status_code, 403)
        self.assertEqual(self.browser.post('/api/me/invite', headers=self.site_headers,
            json={'code': m.users.get_user('other_user')['invite_code']}).status_code, 403)
        self.assertEqual(self.browser.post('/api/payment/orders', headers=self.site_headers,
            json={'package_id': 'points_600', 'code': 'fixture', 'client_key': 'fixture_checkout'}).status_code, 403)
        self.assertEqual(m.users.get_balance(identity), 40)

    def test_site_binding_disabled_has_no_network_or_new_account(self):
        self.register(); before = m.users._conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]
        info = self.browser.get('/api/auth/site/session').json()
        self.assertFalse(info['wechat_login']['ready']); self.assertFalse(info['wechat_bound'])
        self.assertEqual(self.browser.post('/api/auth/site/wechat/start', headers=self.site_headers).status_code, 503)
        self.assertEqual(m.users._conn.execute('SELECT COUNT(*) FROM users').fetchone()[0], before)

    def test_site_oauth_and_qrconnect_use_server_redirect_scopes_and_one_use_state(self):
        self.register(); self.configured('official')
        state, official = self.start_state()
        self.assertTrue(official['url'].startswith('https://open.weixin.qq.com/connect/oauth2/authorize?'))
        self.assertIn('scope=snsapi_userinfo', official['url'])
        self.configured('open'); _, opened = self.start_state()
        self.assertTrue(opened['url'].startswith('https://open.weixin.qq.com/connect/qrconnect?'))
        self.assertIn('scope=snsapi_login', opened['url'])
        self.assertNotIn('fixture-secret', opened['url'])
        self.assertEqual(self.callback(state).status_code, 403)  # Application changed after start.

    def test_site_oauth_callback_owner_state_expiry_and_replay_guard(self):
        self.register(); self.configured(); state, _ = self.start_state()
        other = TestClient(m.app, base_url='https://image.myil.top')
        try:
            other.post('/api/auth/site/register', headers=self.site_headers,
                       json={'username': 'other_site_user', 'password': 'fixture-password-01'})
            self.assertEqual(other.get('/api/auth/site/wechat/callback?state=' + state + '&code=fixture').status_code, 403)
        finally: other.close()
        self.assertEqual(self.callback(state).status_code, 303)
        self.assertEqual(self.callback(state).status_code, 403)
        expired, _ = self.start_state()
        m.users._conn.execute('UPDATE site_oauth_states SET expires_at=? WHERE state_hash=?', (time.time() - 1, token_hash(expired)))
        m.users._conn.commit()
        self.assertEqual(self.callback(expired).status_code, 403)

    def test_site_verified_unionid_binds_only_existing_mini_and_syncs_both_histories(self):
        initial = self.register(); web = self.account_id(); self.configured()
        m.users.admin_adjust_balance(web, delta=25)
        m.users.ensure_user('verified_mini', account_type='wechat', app_id='mini_app')
        register_verified(m.users, 'mini_app', 'verified_mini', 'verified_union', 'mini')
        m.jobs.create('aabbcc000011', openid=web, status='succeeded', completed_at=time.time(), result_file='result_aabbcc000011.jpg')
        m.jobs.create('aabbcc000012', openid='verified_mini', status='succeeded', completed_at=time.time())
        state, _ = self.start_state(); result = self.callback(state)
        self.assertEqual(result.status_code, 303, result.text); self.assertIn('wechat_binding=bound', result.headers['location'])
        session = self.browser.get('/api/auth/site/session').json()
        self.assertTrue(session['wechat_bound']); self.assertEqual(session['balance'], 125)
        self.assertEqual(session['account_user_id'], initial['account_user_id'])
        self.assertNotEqual(session['user_id'], initial['user_id'])
        self.assertEqual(self.browser.get('/api/my/jobs').json()['total'], 2)
        mini = self.client.get('/api/my/jobs', headers={'Authorization': 'Bearer ' + m.user_token('verified_mini')}).json()
        self.assertEqual(mini['total'], 2)
        self.assertEqual(self.browser.get('/api/jobs/aabbcc000011').status_code, 200)
        self.assertEqual(self.client.get('/api/jobs/aabbcc000011', headers=self.other_headers()).status_code, 404)
        self.assertEqual(m.users._conn.execute('SELECT balance FROM users WHERE openid=?', (web,)).fetchone()[0], 0)
        self.assertEqual(m.users.get_balance(web), 125)
        self.assertEqual(self.client.get('/api/me',headers={'Authorization':'Bearer '+m.user_token(web)}).status_code,401)
        m.users.admin_adjust_balance(web, delta=10); self.assertEqual(m.users.get_balance('verified_mini'), 135)
        self.assertEqual(self.browser.post('/api/me/earn', headers=self.site_headers, json={'kind': 'checkin'}).status_code, 403)

    def other_headers(self):
        return {'Authorization': 'Bearer ' + m.user_token('other_user')}

    def test_site_no_unionid_never_guesses_openid_or_binds(self):
        self.register(); self.configured(); state, _ = self.start_state()
        response = type('FixtureResponse', (), {'status_code': 200, 'json': lambda self: {'openid': 'official_verified', 'access_token': 'fixture'}})()
        with patch.object(web_wechat.requests, 'get', return_value=response):
            result = self.browser.get('/api/auth/site/wechat/callback?state=' + state + '&code=fixture', follow_redirects=False)
        self.assertEqual(result.status_code, 409)
        self.assertFalse(self.browser.get('/api/auth/site/session').json()['wechat_bound'])
        self.assertEqual(m.users._conn.execute('SELECT COUNT(*) FROM account_aliases').fetchone()[0], 0)

    def test_site_missing_mini_mapping_waits_for_real_login_without_new_gift(self):
        self.register(); self.configured(); web = self.account_id(); state, _ = self.start_state()
        before = m.users._conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]
        result = self.callback(state); self.assertEqual(result.status_code, 303)
        self.assertIn('pending_mini_identity', result.headers['location'])
        self.assertEqual(m.users._conn.execute('SELECT COUNT(*) FROM users').fetchone()[0], before)
        self.assertEqual(m.users.get_balance(web), 0)
        with patch.object(wechat_auth, 'exchange_session', return_value={'openid': 'new_real_mini', 'unionid': 'verified_union', 'session_key': 'not-exposed'}):
            login = self.client.post('/api/auth/login', json={'code': 'fixture', 'app_id': 'mini_app'})
        self.assertEqual(login.status_code, 200, login.text)
        self.assertEqual(login.json()['balance'], 0)  # Explicit site registration cannot farm a second welcome grant.
        self.assertEqual(canonical(m.users, web), 'new_real_mini')
        self.assertTrue(self.browser.get('/api/auth/site/session').json()['wechat_bound'])

    def test_site_local_media_and_recipe_authorize_original_alias_after_link(self):
        self.register(); web = self.account_id(); m.users.ensure_user('verified_mini')
        register_verified(m.users, 'mini_app', 'verified_mini', 'verified_union', 'mini')
        name = 'orig_aabbcc000014.jpg'; result = 'result_aabbcc000014.jpg'
        Path(m.UPLOAD_DIR, name).write_bytes(self.image()); Path(m.UPLOAD_DIR, result).write_bytes(self.image())
        m.jobs.create('aabbcc000014', openid=web, status='succeeded', completed_at=time.time(), orig_file=name,
            result_file=result, recipe={'input_mode': 'photo', 'quality': 'fine', 'template_id': '', 'text_fields': {},
                'custom_prompt': '', 'aspect_ratio': '', 'template_output_mode': '', 'prompt': ''})
        bind_verified(m.users, web, 'verified_mini', 'verified_union', 'mini_app')
        self.assertEqual(self.browser.get('/api/images/' + name).status_code, 200)
        self.assertEqual(self.browser.get('/api/jobs/aabbcc000014/recipe').json()['recipe_complete'], True)
        mini_headers = {'Authorization': 'Bearer ' + m.user_token('verified_mini')}
        self.assertEqual(self.client.get('/api/images/' + name, headers=mini_headers).status_code, 200)

    def test_site_static_assets_only_mount_dedicated_directory(self):
        directory = Path(m.BASE_DIR, 'static', 'web'); directory.mkdir(parents=True, exist_ok=True)
        shutil.copytree(ROOT/'backend/static/web',directory,dirs_exist_ok=True)
        (directory / 'fixture.txt').write_text('fixture asset', encoding='utf-8')
        html=self.browser.get('/');self.assertEqual(html.status_code,200)
        resources=re.findall(r'(?:href|src)=["\'](/web-assets/[^"\']+)["\']',html.text)
        self.assertEqual(len(resources),4)
        for resource in resources:
            response=self.browser.get(resource);self.assertEqual(response.status_code,200,resource)
            if resource.endswith('.js'):self.assertIn('javascript',response.headers['content-type'])
        for module in ('boot.js','app.js','creation.js','library.js'):
            response=self.browser.get('/web-assets/'+module)
            self.assertEqual(response.status_code,200,module);self.assertIn('javascript',response.headers['content-type'])
            imports=re.findall(r'["\'](\./[^"\']+\.js)["\']',response.text)
            for relative in imports:
                loaded=self.browser.get('/web-assets/'+relative[2:]);self.assertEqual(loaded.status_code,200,relative)
                self.assertIn('javascript',loaded.headers['content-type'])
        # Delivery now exports only JS/CSS/SVG; unrelated files stay inaccessible.
        self.assertEqual(self.browser.get('/web-assets/fixture.txt').status_code, 404)
        self.assertEqual(self.browser.get('/web-assets/../../user_store.py').status_code, 404)
        self.assertEqual(self.browser.get('/web-assets/%2e%2e/%2e%2e/user_store.py').status_code, 404)

    def test_site_refund_i64_overflow_rolls_back_flag_audit_and_retries_once(self):
        m.users.reserve_job('sample_user','aabbcc000030',10,{})
        m.users.admin_adjust_balance('sample_user',balance=2**63-1)
        count=m.users._conn.execute('SELECT COUNT(*) FROM audit').fetchone()[0]
        with self.assertRaises(ValueError):m.users.refund_job('sample_user','aabbcc000030')
        self.assertEqual(m.users.get_balance('sample_user'),2**63-1)
        self.assertEqual(m.users._conn.execute('SELECT typeof(balance) FROM users WHERE openid=?',('sample_user',)).fetchone()[0],'integer')
        self.assertEqual(m.users._conn.execute('SELECT refunded FROM job_charges WHERE job_id=?',('aabbcc000030',)).fetchone()[0],0)
        self.assertEqual(m.users._conn.execute('SELECT COUNT(*) FROM audit').fetchone()[0],count)
        m.users.admin_adjust_balance('sample_user',balance=100)
        m.users.refund_job('sample_user','aabbcc000030');m.users.refund_job('sample_user','aabbcc000030')
        self.assertEqual(m.users.get_balance('sample_user'),110)
        self.assertEqual(m.users._conn.execute("SELECT COUNT(*) FROM audit WHERE action='refund' AND detail LIKE 'job=aabbcc000030 %'").fetchone()[0],1)

    def test_site_rewards_and_appeal_i64_overflow_never_consume_claim(self):
        m.users.admin_adjust_balance('sample_user',balance=2**63-1)
        with self.assertRaises(ValueError):m.users.claim_checkin('sample_user',10,0)
        with self.assertRaises(ValueError):m.users.claim_video_reward('sample_user','max_reward_fixture',10,3)
        with self.assertRaises(ValueError):m.users.earn('sample_user','fixture_reward',10,3)
        self.assertFalse(m.users.checkin_status('sample_user')['checkin_done'])
        self.assertIsNone(m.users._conn.execute('SELECT 1 FROM ad_rewards WHERE event_id=?',('max_reward_fixture',)).fetchone())
        self.assertEqual(m.users.get_balance('sample_user'),2**63-1)
        m.users.admin_adjust_balance('sample_user',balance=100)
        m.users.record_violation('sample_user','max_appeal_fixture','image','fixture',7)
        m.users.admin_adjust_balance('sample_user',balance=2**63-1)
        with self.assertRaises(ValueError):m.users.resolve_violation('max_appeal_fixture',True)
        self.assertEqual(m.users._conn.execute('SELECT status FROM violations WHERE id=?',('max_appeal_fixture',)).fetchone()[0],'active')
        m.users.admin_adjust_balance('sample_user',balance=100)
        m.users.resolve_violation('max_appeal_fixture',True);m.users.resolve_violation('max_appeal_fixture',True)
        self.assertEqual(m.users.get_balance('sample_user'),107)

    def test_site_strict_integer_writes_and_malformed_balance_are_not_truncated(self):
        for invalid in (True,1.25,'8'):
            with self.assertRaises(ValueError):m.users.add_balance('sample_user',invalid)
            with self.assertRaises(ValueError):m.users.set_balance('sample_user',invalid)
        self.assertEqual(m.users.get_balance('sample_user'),100)
        m.users._conn.execute('UPDATE users SET balance=1.25 WHERE openid=?',('sample_user',));m.users._conn.commit()
        for operation in (lambda:m.users.add_balance('sample_user',1),lambda:m.users.set_balance('sample_user',10),
                          lambda:m.users.admin_adjust_balance('sample_user',delta=1)):
            with self.assertRaises(ValueError):operation()
        self.assertEqual(m.users._conn.execute('SELECT balance,typeof(balance) FROM users WHERE openid=?',('sample_user',)).fetchone(),(1.25,'real'))

    def test_site_mini_app_change_invalidates_state_not_site_password_session(self):
        self.register();self.configured();web=self.account_id()
        m.users.ensure_user('typed_mini',account_type='wechat',app_id='mini_app')
        register_verified(m.users,'mini_app','typed_mini','typed_union','mini')
        bind_verified(m.users,web,'typed_mini','typed_union','site_app',mini_app_id='mini_app')
        state,_=self.start_state();m.settings.update({'wechat':{'app_id':'different_mini'}})
        self.assertEqual(self.callback(state).status_code,403)
        self.assertEqual(self.browser.get('/api/auth/site/session').status_code,200)
        self.assertEqual(self.browser.get('/api/me').status_code,200)
        self.assertEqual(self.client.get('/api/me',headers={'Authorization':'Bearer '+m.user_token('typed_mini')}).status_code,401)

    def test_site_alias_refund_cancel_and_both_sides_spend_one_shared_ledger(self):
        self.register();web=self.account_id();m.users.admin_adjust_balance(web,delta=100)
        m.users.reserve_job(web,'aabbcc000021',10,{});m.users.confirm_job('aabbcc000021','fixture')
        m.users.ensure_user('ledger_mini');register_verified(m.users,'mini_app','ledger_mini','ledger_union','mini')
        bind_verified(m.users,web,'ledger_mini','ledger_union','mini_app')
        self.assertEqual(m.users.get_user(web)['total_jobs'],1)
        m.users.refund_job(web,'aabbcc000021',cancel=True);m.users.refund_job('ledger_mini','aabbcc000021',cancel=True)
        self.assertEqual(m.users.get_balance(web),200);self.assertEqual(m.users.get_user(web)['total_jobs'],0)
        m.users.reserve_job(web,'aabbcc000022',40,{});m.users.reserve_job('ledger_mini','aabbcc000023',40,{})
        self.assertEqual(m.users.get_balance(web),120);self.assertEqual(m.users.get_balance('ledger_mini'),120)
        self.assertEqual(m.users._conn.execute('SELECT balance FROM users WHERE openid=?',(web,)).fetchone()[0],0)
        refunds=m.users._conn.execute("SELECT COUNT(*) FROM audit WHERE action='refund' AND detail LIKE 'job=aabbcc000021 %'").fetchone()[0]
        self.assertEqual(refunds,1)

    def test_site_cross_connection_bind_adjust_refund_concurrency_and_restart_preserve_money(self):
        self.register();web=self.account_id();m.users.admin_adjust_balance(web,delta=100)
        m.users.reserve_job(web,'aabbcc000024',40,{})
        m.users.ensure_user('concurrent_mini');register_verified(m.users,'mini_app','concurrent_mini','concurrent_union','mini')
        secondary=UserStore(str(self.d))
        try:
            with ThreadPoolExecutor(max_workers=3) as executor:
                futures=[executor.submit(bind_verified,m.users,web,'concurrent_mini','concurrent_union','mini_app'),
                         executor.submit(secondary.admin_adjust_balance,web,delta=20),
                         executor.submit(secondary.refund_job,web,'aabbcc000024')]
                for future in futures:future.result()
            self.assertEqual(m.users.get_balance(web),220);self.assertEqual(secondary.get_balance('concurrent_mini'),220)
            self.assertEqual(secondary._conn.execute('SELECT balance FROM users WHERE openid=?',(web,)).fetchone()[0],0)
        finally:secondary.close()
        reopened=UserStore(str(self.d))
        try:
            self.assertEqual(reopened.get_balance(web),220)
            again=bind_verified(reopened,web,'concurrent_mini','concurrent_union','mini_app')
            self.assertTrue(again['already_bound']);self.assertEqual(again['transferred_amount'],0)
            self.assertEqual(reopened.get_balance(web),220)
        finally:reopened.close()

    def test_site_alias_violation_history_and_feedback_are_one_enforcement_subject(self):
        self.register();web=self.account_id();m.users.admin_adjust_balance(web,delta=100)
        for index in range(2):m.users.record_violation(web,'alias_old_'+str(index),'image','fixture',7)
        m.users.ensure_user('penalty_mini');register_verified(m.users,'mini_app','penalty_mini','penalty_union','mini')
        bind_verified(m.users,web,'penalty_mini','penalty_union','mini_app')
        penalty=m.users.record_violation('penalty_mini','alias_new','image','fixture',7)
        self.assertEqual(penalty['weekly_count'],3);self.assertTrue(penalty['banned'])
        self.assertTrue(m.users.get_user(web)['banned'])
        self.assertTrue(m.users.submit_violation_feedback('penalty_mini','alias_old_0','fixture review'))
        m.users.resolve_violation('alias_old_0',True)
        self.assertEqual(m.users.get_balance(web),186)
        self.assertEqual(m.users._conn.execute('SELECT balance FROM users WHERE openid=?',(web,)).fetchone()[0],0)

    def test_site_session_survives_restart_and_expiry_does_not_create_guest(self):
        data=self.register();identity=self.account_id();reopened=UserStore(str(self.d))
        try:
            session=store_for(reopened).session(data['token']);self.assertEqual(session['account_id'],identity)
            reopened._conn.execute('UPDATE site_sessions SET expires_at=? WHERE token_hash=?',(time.time()-1,token_hash(data['token'])))
            reopened._conn.commit()
        finally:reopened.close()
        self.assertEqual(self.browser.get('/api/me').status_code,401)
        self.assertEqual(m.users.get_balance(identity),0)
        self.assertEqual(m.users._conn.execute('SELECT COUNT(*) FROM web_credentials').fetchone()[0],1)

    def test_site_loopback_cookie_works_without_weakening_https_upstream_cookie(self):
        local=TestClient(m.app,base_url='http://127.0.0.1:8000')
        try:
            response=local.post('/api/auth/site/register',headers={'X-Site-Request':'1','Origin':'http://127.0.0.1:8000'},
                                json={'username':'local_fixture','password':'fixture-password-01'})
            self.assertEqual(response.status_code,200,response.text)
            self.assertNotIn('Secure',response.headers['set-cookie'])
            self.assertEqual(local.get('/api/me').status_code,200)
        finally:local.close()
        class Upstream:
            async def __call__(self,scope,receive,send):
                if scope['type']=='http':scope={**scope,'scheme':'http','server':('127.0.0.1',8000),
                    'headers':[(key,value) for key,value in scope['headers'] if key.lower()!=b'host']+[(b'host',b'127.0.0.1:8000')]}
                await m.app(scope,receive,send)
        upstream=TestClient(Upstream(),base_url='https://image.myil.top')
        try:
            response=upstream.post('/api/auth/site/register',headers=self.site_headers,
                                   json={'username':'upstream_fixture','password':'fixture-password-01'})
            self.assertEqual(response.status_code,200,response.text);self.assertIn('Secure',response.headers['set-cookie'])
            self.assertEqual(upstream.get('/api/me').status_code,200)
        finally:upstream.close()


if __name__ == '__main__':
    suite = unittest.TestSuite(SiteTests(name) for name in SiteTests.__dict__ if name.startswith('test_site_'))
    names = [case._testMethodName for case in suite]
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    failed = {case._testMethodName for case, _ in result.failures + result.errors}
    (OUTPUT / 'site_accounts_results.json').write_text(json.dumps({'cases': [
        {'case': name, 'passed': name not in failed} for name in names]}, indent=2), encoding='utf-8')
    for connection in initial_connections:
        try: connection.close()
        except Exception: pass
    print('SITE_ACCOUNTS_SUMMARY total=%d passed=%d failed=%d' % (result.testsRun, result.testsRun - len(failed), len(failed)))
    raise SystemExit(0 if result.wasSuccessful() else 1)
