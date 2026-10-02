"""Offline load-path regressions: real fixture SQLite, no live data or network."""
import copy
import json
import re
import sqlite3
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from test_workflow import WorkflowTests, m, OUTPUT, initial_connections
from account_links import aliases_unlocked
from experience_store import store_for
import read_projection
from payment_store import PaymentStore


def legacy_records(users, openid):
    """Independent oracle of the pre-optimization historical record rules."""
    records = []
    def add(identity, kind, title, amount, at, job_id=None, order_id=None):
        if type(amount) is int and amount:
            records.append(dict(id=identity, kind=kind, title=title, amount=amount,
                                created_at=at, job_id=job_id, order_id=order_id))
    with users._lock:
        db = users._conn; owners = aliases_unlocked(users, openid)
        clause = ' IN (' + ','.join('?' for _ in owners) + ')'
        for jid, amount, at in db.execute('SELECT job_id,amount,created_at FROM job_charges WHERE openid'+clause, owners):
            add('charge:'+jid, 'generation', '生成作品', -int(amount), at, jid)
        violations = dict(db.execute('SELECT id,charged FROM violations WHERE openid'+clause, owners))
        for aid, at, action, detail in db.execute("SELECT id,ts,action,detail FROM audit WHERE openid"+clause+" AND action IN "
                "('refund','earn_checkin','earn_video','community_featured','blocked','violation_review','admin_adjust_balance')", owners):
            amount = None; title = ''; jid = None
            job = re.search(r'(?:^|\s)job=([A-Za-z0-9_-]+)', detail)
            if job: jid = job.group(1)
            plus = re.search(r'(?:^|\s)\+=(\d+)|(?:^|\s)\+(\d+)', detail)
            if action == 'refund' and jid and plus:
                amount = int(plus.group(1) or plus.group(2)); title = '生成退款'
            elif action in ('earn_checkin','earn_video','community_featured') and plus:
                amount = int(plus.group(1) or plus.group(2))
                title = {'earn_checkin':'签到奖励','earn_video':'视频奖励','community_featured':'社区精选奖励'}[action]
            elif action == 'blocked':
                charged = re.search(r'(?:^|\s)charged=(\d+)', detail)
                if charged: amount = -int(charged.group(1)); title = '审核扣除'
            elif action == 'violation_review' and 'status=overturned' in detail:
                identity = re.search(r'(?:^|\s)id=([A-Za-z0-9_-]+)', detail)
                if identity and identity.group(1) in violations:
                    amount = int(violations[identity.group(1)]); title = '审核申诉退款'
            elif action == 'admin_adjust_balance':
                delta = re.search(r'(?:^|\s)delta=(-?\d+)(?:\s|$)', detail)
                if delta: amount = int(delta.group(1)); title = '光子调整'
            if amount is not None: add('audit:'+str(aid), action, title, amount, at, jid)
        for invitee, inviter, at, reward in db.execute('SELECT invitee,inviter,created_at,reward FROM invite_bindings WHERE invitee'+clause+' OR inviter'+clause, (*owners,*owners)):
            if reward is not None:
                import hashlib
                identity = hashlib.sha256((openid+'\0'+invitee).encode()).hexdigest()[:20]
                add('invite:'+identity, 'invite', '邀请奖励', int(reward), at)
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='payment_ledger'").fetchone():
            for lid, oid, action, delta, at in db.execute('SELECT id,order_id,action,delta,created_at FROM payment_ledger WHERE openid'+clause, owners):
                add('payment:'+str(lid), 'payment_'+action, '充值到账' if action=='credit' else '充值退款扣回', int(delta), at, order_id=oid)
    return sorted(records, key=lambda row:(row['created_at'],row['id']), reverse=True)


observations = []


class LoadBackendTests(WorkflowTests):
    def linked_alias(self):
        identity = 'web-fixture-alias'
        m.users.ensure_user(identity, account_type='web', welcome_balance=0)
        with m.users._lock, m.users._conn:
            m.users._conn.execute('INSERT INTO account_aliases VALUES(?,?,?,?,?)',
                                 (identity, 'sample_user', time.time(), 'fixture-union', 'fixture-app'))
        return identity

    def assert_indexes(self):
        expected = {}
        processing = set()
        for job in m.jobs._data.values():
            if job.get('deleted_at'): continue
            expected.setdefault(job.get('openid'), {}).setdefault(job.get('status'), set()).add(job['id'])
            if job.get('status') == 'processing': processing.add(job['id'])
        self.assertEqual(m.jobs._by_owner, expected)
        self.assertEqual(m.jobs._processing, processing)

    def test_job_indexes_all_transitions_owner_changes_and_persist_failure(self):
        jobs = m.jobs
        jobs.create('index001', openid='sample_user', cloud_pipeline=True, cloud_phase='submit')
        self.assert_indexes()
        jobs.begin_cloud_submission('index001'); self.assert_indexes()
        jobs.fail_cloud_job('index001', error='fixture'); self.assert_indexes()
        jobs.update('index001', openid='other_user', status='succeeded'); self.assert_indexes()
        jobs.create('index001', openid='sample_user', status='succeeded'); self.assert_indexes()
        before = copy.deepcopy(jobs._by_owner); data = copy.deepcopy(jobs._data)
        with patch.object(jobs, '_persist', side_effect=sqlite3.OperationalError('FIXTURE_PERSIST_FAILURE')):
            for operation in (
                lambda: jobs.create('failednew', openid='sample_user'),
                lambda: jobs.update('index001', status='failed'),
                lambda: jobs.delete_for_openid('index001', 'sample_user')):
                with self.assertRaises(sqlite3.OperationalError): operation()
        self.assertEqual(jobs._by_owner, before); self.assertEqual(jobs._data, data)
        jobs.delete_for_openid('index001', 'sample_user'); self.assert_indexes()
        jobs.update('index001', status='succeeded'); self.assert_indexes()
        self.assertEqual(jobs.counts_for_openid('sample_user')['total'], 0)

    def test_job_cloud_boundary_persist_failure_keeps_index_and_phase(self):
        m.jobs.create('boundary', openid='sample_user', cloud_pipeline=True, cloud_phase='submit')
        before = m.jobs.get('boundary')
        with patch.object(m.jobs, '_persist', side_effect=sqlite3.OperationalError('FIXTURE_PERSIST_FAILURE')):
            with self.assertRaises(sqlite3.OperationalError): m.jobs.begin_cloud_submission('boundary')
            with self.assertRaises(sqlite3.OperationalError): m.jobs.fail_cloud_job('boundary')
        self.assertEqual(m.jobs.get('boundary'), before); self.assert_indexes()

    def test_job_index_aliases_filter_pagination_and_no_global_scan(self):
        alias = self.linked_alias(); now = time.time()
        for index in range(120):
            m.jobs.create('foreign%04d'%index, openid='other_user', status='succeeded', created_at=now-index)
        for index, state in enumerate(('succeeded','failed','processing','succeeded')):
            m.jobs.create('owned%04d'%index, openid=alias if index%2 else 'sample_user',
                          status=state, cloud_pipeline=True, created_at=now-index)
        class NoGlobalScan(dict):
            def values(self): raise AssertionError('GLOBAL_HISTORY_SCAN')
            def items(self): raise AssertionError('GLOBAL_HISTORY_SCAN')
        original = m.jobs._data; m.jobs._data = NoGlobalScan(original)
        try:
            page, counts = m.jobs.page_for_openid('sample_user', 1, 1, 'succeeded')
            self.assertEqual([row['id'] for row in page], ['owned0003'])
            self.assertEqual(counts['total'], 4)
            self.assertEqual(counts['status_counts'], {'processing':1, 'succeeded':2, 'failed':1})
            self.assertEqual(m.jobs.counts_for_openid(alias), counts)
            self.assertEqual(len(m.jobs.list_for_openid(alias)), 4)
        finally: m.jobs._data = original
        observations.append({'case':'120 foreign jobs + 4 alias-owned jobs', 'global_history_scans':0})

    def test_job_index_expiry_purge_restore_reload_and_delete_failure(self):
        jobs = m.jobs
        jobs.create('kept', openid='sample_user', status='succeeded')
        jobs.create('expired', openid='sample_user', status='succeeded', created_at=time.time()-jobs._ttl-1)
        before = copy.deepcopy(jobs._by_owner)
        with patch.object(jobs, '_delete_rows', side_effect=sqlite3.OperationalError('FIXTURE_DELETE_FAILURE')):
            with self.assertRaises(sqlite3.OperationalError): jobs.sweep()
            self.assertEqual(jobs._by_owner, before); self.assert_indexes()
            with self.assertRaises(sqlite3.OperationalError): jobs.take_all_for_purge()
            self.assertEqual(jobs._by_owner, before)
        jobs.sweep(); self.assertIsNone(jobs.get('expired')); self.assert_indexes()
        snapshot = jobs.take_all_for_purge(); self.assert_indexes()
        self.assertEqual(jobs._by_owner, {})
        jobs.restore_purged(snapshot); self.assert_indexes()
        jobs._reload(); self.assert_indexes()
        self.assertEqual(jobs.counts_for_openid('sample_user')['total'], 1)

    def test_job_index_concurrent_create_update_delete_reads_consistent(self):
        def operation(index):
            jid = 'thread%04d'%index
            m.jobs.create(jid, openid='sample_user', cloud_pipeline=True)
            m.jobs.update(jid, status='succeeded')
            if index%3 == 0: m.jobs.delete_for_openid(jid, 'sample_user')
            page, counts = m.jobs.page_for_openid('sample_user', limit=100)
            self.assertEqual(len(page), counts['total'])
            self.assertEqual(sum(counts['status_counts'].values()), counts['total'])
        with ThreadPoolExecutor(max_workers=8) as pool: list(pool.map(operation, range(48)))
        self.assert_indexes(); self.assertEqual(m.jobs.counts_for_openid('sample_user')['total'], 32)

    def test_my_jobs_one_index_snapshot_and_one_ledger_select(self):
        alias = self.linked_alias()
        for index in range(12):
            jid = 'batch%04d'%index
            m.users.reserve_job(alias, jid, 1, {})
            m.jobs.create(jid, openid=alias, status='succeeded')
        traced = []; m.users._conn.set_trace_callback(traced.append)
        try:
            with patch.object(m.jobs, 'page_for_openid', wraps=m.jobs.page_for_openid) as page, \
                    patch.object(m.jobs, 'counts_for_openid', side_effect=AssertionError('SECOND_SUMMARY_SCAN')), \
                    patch.object(store_for(m.users), 'settlement', side_effect=AssertionError('PER_JOB_LEDGER_QUERY')):
                response = self.client.get('/api/my/jobs?limit=12', headers=self.headers)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(page.call_count, 1)
        finally: m.users._conn.set_trace_callback(None)
        selects = [sql for sql in traced if sql.startswith('SELECT job_id,amount,refunded,state FROM job_charges')]
        self.assertEqual(len(selects), 1)
        self.assertTrue(all(job['charged_amount']==1 for job in response.json()['jobs']))
        observations.append({'case':'12-card /api/my/jobs', 'index_snapshots':1, 'settlement_selects':len(selects)})

    def test_job_thumbnail_private_owner_expiry_and_original_urls(self):
        import cos_store
        jid = self.job(result_cos='results/fixture.jpg')
        with patch.object(m.settings, 'cos_ready', return_value=True), \
                patch.object(m, 'cos_presign', return_value='https://fixture.invalid/full'), \
                patch.object(cos_store, 'thumbnail_url', return_value='https://fixture.invalid/thumb') as thumb:
            detail = self.client.get('/api/jobs/'+jid, headers=self.headers).json()
            page = self.client.get('/api/my/jobs', headers=self.headers).json()['jobs'][0]
            self.assertEqual((detail['thumb_url'], page['thumb_url']), ('https://fixture.invalid/thumb',)*2)
            self.assertEqual(detail['result_url'], 'https://fixture.invalid/full')
            self.assertEqual(thumb.call_args.kwargs['width'], 480)
            self.assertLessEqual(thumb.call_args.kwargs['ttl_seconds'], m.MEDIA_URL_TTL_SECONDS)
            other = {'Authorization':'Bearer '+m.user_token('other_user')}
            self.assertEqual(self.client.get('/api/jobs/'+jid, headers=other).status_code, 404)
            self.assertEqual(len(self.client.get('/api/my/jobs', headers=other).json()['jobs']), 0)
            m.jobs.update(jid, completed_at=time.time()-m.JOB_TTL_SECONDS+20)
            self.client.get('/api/jobs/'+jid, headers=self.headers)
            self.assertLessEqual(thumb.call_args.kwargs['ttl_seconds'], 20)
            for change in ({'completed_at':time.time()-m.JOB_TTL_SECONDS-1}, {'status':'processing'}, {'deleted_at':time.time()}):
                m.jobs.update(jid, **change)
                self.assertIsNone(m._job_thumbnail_url(m.jobs.get(jid)))

    def test_public_config_normalized_hot_read_avoids_full_snapshot(self):
        # Migrate the old schema once, then every ordinary read uses scalar fields.
        self.client.get('/api/config')
        with m.settings._lock:
            m.settings._data['community']['items'] = [{'story':'x'*900} for _ in range(200)]
        with patch.object(m.settings, 'snapshot', side_effect=AssertionError('FULL_CONFIGURATION_COPY')):
            first = self.client.get('/api/config'); self.assertEqual(first.status_code, 200, first.text)
            self.assertTrue(first.json()['community']['enabled'])
        m.settings.update({'prices':{'light':57,'fine':58}, 'processing':{'ci_enabled':True}})
        fresh = self.client.get('/api/config').json()
        self.assertEqual(fresh['prices'], {'light':57,'fine':58}); self.assertTrue(fresh['image_processing']['ci_enabled'])
        observations.append({'case':'public config with 200 community posts', 'full_settings_snapshots':0})

    def test_credit_database_pagination_matches_every_legacy_rule(self):
        store = store_for(m.users); alias = self.linked_alias(); db = m.users._conn
        PaymentStore(m.users)
        cases = [('refund','job=abc_1 +=12'), ('refund','job=def-2 +3'), ('refund','job=missing'),
                 ('earn_checkin','+13'), ('earn_video','prefix +=14'), ('community_featured','+15'),
                 ('blocked','kind=image charged=7'), ('blocked','kind=image charged=0'),
                 ('violation_review','id=review_good status=overturned'), ('violation_review','id=not_exists status=overturned'),
                 ('violation_review','id=review_good status=upheld'), ('admin_adjust_balance','delta=-9 balance=42'),
                 ('admin_adjust_balance','delta=0'), ('admin_adjust_balance','delta=None balance=9999'),
                 ('admin_adjust_balance','prefixdelta=22'), ('admin_adjust_balance','delta=123bad'),
                 ('admin_adjust_balance','delta='+str(2**80)), ('earn_video','abc+18'), ('submitted','job=non_credit')]
        with m.users._lock, db:
            db.execute('INSERT INTO violations VALUES(?,?,?,?,?,?,?,?)', ('review_good', alias, 42, 'text', 'fixture', 11, 'overturned',''))
            db.executemany('INSERT INTO audit(ts,openid,action,detail) VALUES(?,?,?,?)',
                           [(42, alias if index%2 else 'sample_user', action, detail) for index,(action,detail) in enumerate(cases)])
            db.execute('INSERT INTO job_charges(job_id,openid,amount,created_at) VALUES(?,?,?,?)', ('paid',alias,20,42))
            db.execute('INSERT INTO job_charges(job_id,openid,amount,created_at) VALUES(?,?,?,?)', ('free','sample_user',0,42))
            db.executemany('INSERT INTO invite_bindings VALUES(?,?,?,?)', [('invite1',alias,42,23),('invite2',alias,42,None),('invite3',alias,42,0)])
            db.executemany('INSERT INTO payment_ledger VALUES(?,?,?,?,?,?,?)',
                           [(1,'order1',alias,'credit',100,0,42),(2,'order1','sample_user','reverse',-30,30,42)])
        expected = legacy_records(m.users, 'sample_user'); before = db.total_changes
        combined = []
        for offset in range(0, len(expected)+3, 3):
            result = store.credit_records('sample_user', offset=offset, limit=3)
            self.assertEqual(result['items'], expected[offset:offset+3]); self.assertEqual(result['total'], len(expected))
            combined.extend(result['items'])
        self.assertEqual(combined, expected); self.assertEqual(db.total_changes, before)
        self.assertFalse(store.credit_records('sample_user')['history_complete'])
        self.assertEqual(store.credit_record_count('sample_user'), len(expected))

    def test_credit_count_cache_own_writes_external_writes_and_bounded_metadata(self):
        store = store_for(m.users); projection = store._credit_projection
        with patch.object(projection, '_count', wraps=projection._count) as counter:
            self.assertEqual(store.credit_record_count('sample_user'), 0)
            for _ in range(8): self.assertEqual(store.credit_record_count('sample_user'), 0)
            self.assertEqual(counter.call_count, 1)
            m.users.audit('sample_user','earn_video','+3')
            self.assertEqual(store.credit_record_count('sample_user'), 1); self.assertEqual(counter.call_count, 2)
            before = m.users._conn.total_changes
            external = sqlite3.connect(m.users._path)
            try:
                external.execute("INSERT INTO audit(ts,openid,action,detail) VALUES(1,'sample_user','earn_video','+5')")
                external.commit()
                self.assertEqual(m.users._conn.total_changes, before)
                self.assertEqual(store.credit_record_count('sample_user'), 2); self.assertEqual(counter.call_count, 3)
                external.execute("UPDATE audit SET detail='+0' WHERE openid='sample_user'"); external.commit()
                self.assertEqual(store.credit_record_count('sample_user'), 0); self.assertEqual(counter.call_count, 4)
                self.assertEqual(store.credit_records('sample_user')['items'], [])
            finally: external.close()
        for index in range(projection.COUNT_CACHE_SIZE+10): store.credit_record_count('unknown-'+str(index))
        self.assertLessEqual(len(projection._counts), projection.COUNT_CACHE_SIZE)
        self.assertTrue(all(type(value) is int for value in projection._counts.values()))
        observations.append({'case':'9 unchanged credit counts', 'historical_count_queries':1,
                             'cache_max_entries':projection.COUNT_CACHE_SIZE, 'external_writes_observed':True})

    def test_credit_alias_cache_invalidation_and_uncommitted_amounts(self):
        store = store_for(m.users)
        self.assertEqual(store.credit_record_count('sample_user'), 0)
        alias = self.linked_alias(); m.users.audit(alias,'earn_checkin','+13')
        self.assertEqual(store.credit_record_count('sample_user'), 1)
        self.assertEqual(store.credit_records(alias)['items'], legacy_records(m.users,alias))
        db = m.users._conn
        db.execute("INSERT INTO audit(ts,openid,action,detail) VALUES(1,'sample_user','earn_video','+7')")
        self.assertEqual(store.credit_record_count('sample_user'), 2)
        self.assertEqual(len(store.credit_records('sample_user')['items']), 2)
        self.assertTrue(db.in_transaction); db.rollback()
        self.assertEqual(store.credit_record_count('sample_user'), 1)

    def test_credit_sql_limit_and_regex_outside_account_lock(self):
        store = store_for(m.users); statements = []; calls = []
        with m.users._lock, m.users._conn:
            m.users._conn.executemany('INSERT INTO audit(ts,openid,action,detail) VALUES(?,?,?,?)',
                                     [(index,'sample_user','earn_video','+1 fixture='+str(index)) for index in range(600)])
        original_connect = sqlite3.connect; original_parse = read_projection.parse_audit
        def connect(*args, **kwargs):
            db = original_connect(*args, **kwargs); db.set_trace_callback(statements.append); return db
        def parse(*args):
            unlocked = m.users._lock.acquire(blocking=False)
            calls.append(unlocked)
            if unlocked: m.users._lock.release()
            return original_parse(*args)
        with patch.object(read_projection.sqlite3,'connect',side_effect=connect), patch.object(read_projection,'parse_audit',side_effect=parse):
            rows = store.credit_records('sample_user',offset=300,limit=7)
        self.assertEqual((len(rows['items']),rows['total']), (7,600))
        self.assertTrue(calls); self.assertTrue(all(calls))
        self.assertTrue(any('ORDER BY created_at DESC,id DESC LIMIT 7 OFFSET 300' in sql for sql in statements))
        self.assertTrue(any('PRAGMA temp_store=FILE' in sql for sql in statements))
        observations.append({'case':'600 history rows, offset 300, limit 7', 'python_materialized_rows':7,
                             'regex_outside_account_lock':True, 'sqlite_temp_store':'FILE'})

    def test_credit_concurrent_reads_writes_and_me_dedicated_count(self):
        store = store_for(m.users)
        def read(_):
            page = store.credit_records('sample_user',limit=100)
            self.assertEqual(len(page['items']),page['total'])
        def write(index): m.users.audit('sample_user','earn_video','+1 sequence='+str(index))
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(write,index) if index%2 else pool.submit(read,index) for index in range(40)]
            for future in futures: future.result()
        self.assertEqual(store.credit_record_count('sample_user'),20)
        with patch.object(store,'credit_records',side_effect=AssertionError('ME_REBUILDS_HISTORY')):
            response = self.client.get('/api/me',headers=self.headers)
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(response.json()['credit_record_count'],20)


if __name__ == '__main__':
    names = [name for name in LoadBackendTests.__dict__ if name.startswith('test_')]
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(LoadBackendTests(name) for name in names))
    failed = {test._testMethodName for test, _ in result.failures + result.errors}
    (OUTPUT/'load_backend_results.json').write_text(json.dumps({'cases':[{'case':name,'passed':name not in failed} for name in names],
        'observations':observations},ensure_ascii=False,indent=2),encoding='utf-8')
    for connection in initial_connections:
        try: connection.close()
        except Exception: pass
    print('LOAD_BACKEND_SUMMARY total=%d passed=%d failed=%d'%(result.testsRun,result.testsRun-len(failed),len(failed)))
    raise SystemExit(0 if result.wasSuccessful() else 1)
