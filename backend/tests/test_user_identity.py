"""Identity/count regressions against an isolated code copy; no live DB/network."""
from pathlib import Path
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(os.environ.get('REVIEW_ROOT', Path(__file__).resolve().parents[2])).resolve()
OUTPUT = Path(os.environ.get('REVIEW_OUTPUT', ROOT / 'audit' / 'identity-tests')).resolve()
OUTPUT.mkdir(parents=True, exist_ok=True)
fixture = tempfile.TemporaryDirectory(prefix='identity_', dir=OUTPUT)
BASE = Path(fixture.name).resolve()
assert BASE.parent == OUTPUT
source = BASE / 'backend'
source.mkdir()
for path in (ROOT / 'backend').glob('*'):
    if path.is_file() and path.suffix in ('.py', '.html'):
        shutil.copy2(path, source / path.name)
for key in ('FAL_KEY', 'BAIDU_API_KEY', 'BAIDU_SECRET_KEY', 'WX_APPID', 'WX_APP_SECRET',
            'TENCENT_SECRET_ID', 'TENCENT_SECRET_KEY', 'NORMALIZE_LONG_SIDE'):
    os.environ.pop(key, None)
os.environ.update(ADMIN_PASSWORD='identity-test-password', LOG_LEVEL='CRITICAL',
                  DATA_DIR=str(source / 'data'), UPLOAD_DIR=str(source / 'uploads'), WORKERS='4')
sys.path.insert(0, str(source))
import requests
from fastapi.testclient import TestClient
from fastapi import FastAPI
guard = patch.object(requests.sessions.Session, 'request', side_effect=AssertionError('NETWORK_DISABLED'))
guard.start()
import main as m
import admin_api
from user_store import UserStore
from cleanup_store import CleanupStore
from settings_store import SettingsStore

connections = [m.users._conn, m.jobs._conn, m.cleanup._conn]
observations = []


class RemoteAddress:
    def __init__(self, application):
        self.application = application
        self.ip = '203.0.113.10'

    async def __call__(self, scope, receive, send):
        scope = {**scope, 'client': (self.ip, 40000)}
        await self.application(scope, receive, send)


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.data = BASE / self._testMethodName
        self.data.mkdir()
        m.users = UserStore(str(self.data)); connections.append(m.users._conn)
        m.settings = SettingsStore(str(self.data))
        m.jobs = m.JobStore(2592000, 5000, db_path=str(self.data / 'jobs.db')); connections.append(m.jobs._conn)
        m.cleanup = CleanupStore(str(self.data), m.UPLOAD_DIR); connections.append(m.cleanup._conn)
        self.address = RemoteAddress(m.app)
        self.client = TestClient(self.address, raise_server_exceptions=False)

    def tearDown(self):
        self.client.close()

    def log(self, result):
        observations.append({'case': self._testMethodName, **result})

    def test_wechat_relogin_is_one_account_one_gift_fixed_uid(self):
        m.settings.update({'wechat': {'app_id': 'wx_test_a', 'app_secret': 'fixture'}})
        with patch.object(m, 'code2session', return_value='o-fixed-wechat-identity'):
            responses = [self.client.post('/api/auth/login', json={'code':str(i), 'app_id':'wx_test_a'}) for i in range(10)]
        identities = [r.json().get('user_id') for r in responses]
        rows = m.users._conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]
        self.log({'account_rows': rows, 'unique_display_ids':len(set(identities)), 'balance':m.users.get_balance('o-fixed-wechat-identity')})
        self.assertTrue(all(r.status_code == 200 for r in responses))
        self.assertEqual(rows, 1)
        self.assertEqual(m.users.get_balance('o-fixed-wechat-identity'), 100)
        self.assertTrue(identities[0] and identities[0].startswith('WX-'))
        self.assertEqual(len(set(identities)), 1)

    def test_web_rows_excluded_from_wechat_statistics(self):
        m.users.ensure_user('o-wechat-user')
        m.users.ensure_user('web-111111111111')
        m.users.ensure_user('web-222222222222')
        stats = m.users.stats()
        items = m.users.list_users()
        self.log({'stats':stats, 'default_list_count':len(items)})
        self.assertEqual(stats['users_total'], 1)
        self.assertEqual(stats.get('wechat_users_total'), 1)
        self.assertEqual(stats.get('web_users_total'), 2)
        self.assertEqual(stats.get('accounts_total'), 3)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['openid'], 'o-wechat-user')

    def test_wechat_web_refresh_preserves_account_and_expiry_requires_scan(self):
        m.users.ensure_user('o-browser-wechat');m.users.set_balance('o-browser-wechat',143)
        headers={'Authorization':'Bearer '+m.user_token('o-browser-wechat')}
        first=self.client.post('/api/auth/web',headers=headers)
        self.address.ip='198.51.100.80'
        second=self.client.post('/api/auth/web',headers=headers)
        self.assertEqual(first.status_code,200);self.assertEqual(second.status_code,200)
        self.assertEqual(second.json()['balance'],143)
        self.assertEqual(m.verify_user_token(second.json()['token']),'o-browser-wechat')
        import time
        with patch.object(m.time,'time',return_value=time.time()+13*3600):
            self.assertEqual(self.client.post('/api/auth/web',headers=headers).status_code,401)
        self.assertEqual(m.users._conn.execute('SELECT COUNT(*) FROM users').fetchone()[0],1)
        self.log({'same_wechat_account':True,'guest_created':False})

    def test_unauthenticated_browsers_do_not_create_accounts(self):
        first=self.client.post('/api/auth/web')
        other=TestClient(self.address,raise_server_exceptions=False)
        second=other.post('/api/auth/web');other.close()
        self.assertEqual(first.status_code,401);self.assertEqual(second.status_code,401)
        self.assertEqual(m.users._conn.execute('SELECT COUNT(*) FROM users').fetchone()[0],0)
        self.log({'guest_created':False})

    def test_legacy_web_account_is_disabled_but_money_is_preserved(self):
        legacy='web-0123456789ab';m.users.ensure_user(legacy);m.users.set_balance(legacy,245)
        response=self.client.post('/api/auth/web',headers={'Authorization':'Bearer '+m.user_token(legacy)})
        self.assertEqual(response.status_code,401);self.assertEqual(m.users.get_balance(legacy),245)
        self.assertEqual(m.users._conn.execute('SELECT COUNT(*) FROM users').fetchone()[0],1)
        self.log({'disabled':True,'preserved_balance':245})

    def test_appid_mismatch_does_not_create_accounts(self):
        m.settings.update({'wechat':{'app_id':'wx_test_a','app_secret':'fixture'}})
        with patch.object(m, 'code2session', return_value='o-incorrect-app'):
            response = self.client.post('/api/auth/login', json={'code':'fixture','app_id':'wx_test_b'})
        rows = m.users._conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]
        self.log({'http':response.status_code,'rows':rows})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(rows, 0)

    def test_legacy_schema_migration_preserves_all_money_and_jobs(self):
        directory = self.data / 'legacy'; directory.mkdir()
        connection = sqlite3.connect(directory / 'users.db')
        connection.execute('CREATE TABLE users(openid TEXT PRIMARY KEY, created_at REAL NOT NULL, last_seen REAL NOT NULL, total_jobs INTEGER DEFAULT 0, blocked INTEGER DEFAULT 0,balance INTEGER DEFAULT 0,invite_code TEXT DEFAULT "",banned INTEGER DEFAULT 0)')
        for key, balance, jobs in [('o-existing-a',110,2), ('o-existing-b',90,0), ('web-abcdef123456',245,1)]:
            connection.execute('INSERT INTO users(openid,created_at,last_seen,balance,total_jobs) VALUES(?,1,1,?,?)',(key,balance,jobs))
        connection.commit(); connection.close()
        migrated = UserStore(str(directory)); connections.append(migrated._conn)
        values = [(u['openid'],u['balance'],u['total_jobs']) for u in [migrated.get_user(k) for k in ['o-existing-a','o-existing-b','web-abcdef123456']]]
        stats = migrated.stats()
        self.log({'preserved':values,'wechat_count':stats['users_total'],'web_count':stats.get('web_users_total')})
        self.assertEqual(values,[('o-existing-a',110,2),('o-existing-b',90,0),('web-abcdef123456',245,1)])
        self.assertEqual(stats['users_total'],2)
        self.assertEqual(stats.get('web_users_total'),1)

    def test_admin_default_list_and_source_filter_are_consistent(self):
        m.users.ensure_user('o-wechat-user');m.users.ensure_user('web-abcdef123456')
        app = FastAPI()
        app.include_router(admin_api.make_admin_router(settings=m.settings, announcements=m.announcements,
                templates=m.templates, jobs=m.jobs, users=m.users, health_fn=m.health, stats_fn=m._admin_stats))
        with TestClient(app) as admin:
            admin.post('/admin/api/login',json={'password':'identity-test-password'})
            headers={'X-Admin-Request':'1'}
            default=admin.get('/admin/api/users',headers=headers).json()
            web=admin.get('/admin/api/users?account_type=web',headers=headers).json()
            invalid=admin.get('/admin/api/users?account_type=invalid',headers=headers)
        self.log({'default_ids':[r['openid'] for r in default['items']],'web_ids':[r['openid'] for r in web['items']],'invalid_http':invalid.status_code})
        self.assertEqual([r['openid'] for r in default['items']],['o-wechat-user'])
        self.assertEqual([r['openid'] for r in web['items']],['web-abcdef123456'])
        self.assertEqual(invalid.status_code,400)

    def test_forged_cookie_cannot_select_someone_elses_identity(self):
        victim='o-victim';m.users.ensure_user(victim);m.users.set_balance(victim,245)
        other=TestClient(self.address,raise_server_exceptions=False)
        other.cookies.set('rescue_web_identity',victim+'.9999999999.'+'0'*64)
        selected=other.post('/api/auth/web');other.close()
        self.assertEqual(selected.status_code,401);self.assertEqual(m.users.get_balance(victim),245)
        self.log({'victim_preserved':True,'victim_balance':245})

if __name__ == '__main__':
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(IdentityTests)
    names=[test._testMethodName for test in suite]
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    failures=len(result.failures)+len(result.errors)
    failed_names={test._testMethodName for test, _ in result.failures + result.errors}
    record={'total':result.testsRun,'passed':result.testsRun-failures,'failed':failures,'observations':observations,
            'cases':[{'case':name,'passed':name not in failed_names} for name in names]}
    (OUTPUT/'identity_results.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
    print('IDENTITY_SUMMARY total=%d passed=%d failed=%d' % (record['total'],record['passed'],record['failed']))
    m.pool.shutdown(wait=True);guard.stop()
    for connection in connections:
        try:connection.close()
        except Exception:pass
    fixture.cleanup()
    sys.exit(0 if result.wasSuccessful() else 1)
