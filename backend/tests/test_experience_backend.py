"""Experience APIs: isolated account DBs, no external network/model calls."""
import json
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from test_workflow import WorkflowTests, m, OUTPUT, initial_connections
from experience_store import ExperienceStore, store_for
from experience_api import idempotent_submit, submission_view, photo_recipe
from user_store import UserStore
from templates_store import TemplateStore
from community_store import CommunityStore
from community_submissions import store_for as community_for
from payment_store import PaymentStore


class ExperienceTests(WorkflowTests):
    def setUp(self):
        super().setUp()
        with patch.object(TemplateStore, 'ensure_placeholder_covers'):
            m.templates = TemplateStore(str(self.d))
        self.other_headers = {'Authorization': 'Bearer ' + m.user_token('other_user')}

    def text_enabled(self):
        m.settings.update({'text_generation': {'enabled': True, 'model': 'fixture-image', 'price': 40,
            'base_url': 'https://fixture.invalid', 'api_key': 'fixture'},
            'providers': {'worldcodes': {'enabled': True, 'base_url': 'https://fixture.invalid', 'api_key': 'fixture'}}})

    def test_experience_credit_ledger_truth_paging_owner_and_no_mutation(self):
        self.assertEqual(self.client.get('/api/me/credits', headers=self.headers).json()['total'], 0)
        m.users.reserve_job('sample_user', 'aabbcc000001', 40, {})
        m.users.confirm_job('aabbcc000001', 'fixture'); m.users.refund_job('sample_user', 'aabbcc000001')
        m.users.claim_checkin('sample_user', 13, 0)
        m.users.bind_invite_once('sample_user', m.users.get_user('other_user')['invite_code'], 17)
        payment = PaymentStore(m.users)
        order, _ = payment.create('sample_user', 'fixture_order', 'app', 'offer', 0,
                                  {'id': 'fixture', 'amount_fen': 100, 'points': 100})
        payment.apply_verified(order['id'], 'WXfixture', True)
        payment.apply_verified(order['id'], 'WXfixture', True, 50)
        m.users.audit('sample_user', 'admin_adjust_balance', 'delta=None balance=9999')
        before = m.users.get_balance('sample_user')
        rows = self.client.get('/api/me/credits?limit=100', headers=self.headers).json()
        self.assertEqual(rows['total'], 6)
        self.assertEqual(sorted(r['amount'] for r in rows['items']), [-50, -40, 13, 17, 40, 100])
        self.assertTrue(all(type(r['amount']) is int for r in rows['items']))
        self.assertFalse(rows['history_complete'])
        first = self.client.get('/api/me/credits?limit=2', headers=self.headers).json()
        second = self.client.get('/api/me/credits?limit=2&offset=2', headers=self.headers).json()
        self.assertEqual(first['next_offset'], 2); self.assertTrue(first['has_more'])
        self.assertFalse({r['id'] for r in first['items']} & {r['id'] for r in second['items']})
        self.assertEqual(self.client.get('/api/me', headers=self.headers).json()['credit_record_count'], 6)
        other = self.client.get('/api/me/credits', headers=self.other_headers).json()
        self.assertEqual([r['amount'] for r in other['items']], [17])
        self.assertEqual(m.users.get_balance('sample_user'), before)
        self.assertEqual(self.client.get('/api/me/credits').status_code, 401)

    def test_experience_credit_penalty_and_review_use_exact_recorded_amount(self):
        m.users.record_violation('sample_user', 'fixture_violation', 'text', 'fixture', 7)
        m.users.resolve_violation('fixture_violation', True)
        records = store_for(m.users).credit_records('sample_user')['items']
        self.assertEqual(sorted(r['amount'] for r in records), [-7, 7])
        self.assertEqual(m.users.get_balance('sample_user'), 100)
        m.users._conn.execute('INSERT INTO invite_bindings(invitee,inviter,created_at,reward) VALUES(?,?,?,NULL)',
                             ('historical', 'sample_user', time.time()))
        m.users._conn.commit()
        self.assertEqual(store_for(m.users).credit_records('sample_user')['total'], 2)

    def test_experience_preferences_cross_client_durable_validation_and_owner(self):
        tid = m.templates.list_templates(enabled_only=True)[0]['id']
        response = self.client.put('/api/me/preferences', headers=self.headers,
                                   json={'template_id': tid, 'favorite': True})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['template_favorites'], [tid])
        self.client.post('/api/me/recent-template', headers=self.headers, json={'template_id': tid})
        self.assertEqual(self.client.get('/api/me/preferences', headers=self.headers).json()['recent_templates'], [tid])
        self.assertEqual(self.client.get('/api/me/preferences', headers=self.other_headers).json()['template_favorites'], [])
        reopened = UserStore(str(self.d))
        try: self.assertEqual(ExperienceStore(reopened).preferences('sample_user')['template_favorites'], [tid])
        finally: reopened.close()
        self.assertEqual(self.client.put('/api/me/preferences', headers=self.headers,
                                        json={'template_id': 'missing', 'favorite': True}).status_code, 400)
        self.assertEqual(self.client.put('/api/me/preferences', headers=self.headers,
                                        json={'template_id': tid, 'favorite': 'false'}).status_code, 422)
        removed = self.client.put('/api/me/preferences', headers=self.headers,
                                  json={'template_id': tid, 'favorite': False}).json()
        self.assertEqual(removed['template_favorites'], []); self.assertEqual(removed['recent_templates'], [tid])

    def test_experience_recent_bounded_and_unavailable_templates_hidden(self):
        store = store_for(m.users)
        for i in range(25): store.preference('sample_user', 'template_' + str(i), recent=True)
        self.assertEqual(len(store.preferences('sample_user')['recent_templates']), 20)
        self.assertEqual(store.preferences('sample_user', {'template_24'})['recent_templates'], ['template_24'])

    def test_experience_account_purge_clears_preferences_and_submission_receipts(self):
        store = store_for(m.users)
        store.preference('sample_user', 'fixture_template', favorite=True)
        store.claim('sample_user', 'purge_fixture', {'prompt': 'fixture'}, 'text')
        m.users.begin_purge()
        try: m.users.purge_all_accounts()
        finally: m.users.end_purge()
        m.users.ensure_user('sample_user')
        self.assertEqual(store.preferences('sample_user')['template_favorites'], [])
        self.assertIsNone(store.submission('sample_user', 'purge_fixture'))

    def test_experience_job_status_filters_before_pagination_counts_and_settlement(self):
        for index, status in enumerate(('failed', 'succeeded', 'failed', 'processing', 'succeeded')):
            jid = 'aabbcc%06d' % index
            m.users.reserve_job('sample_user', jid, 10, {})
            m.jobs.create(jid, openid='sample_user', status=status, quality='light', stage=status,
                          created_at=time.time() - index, completed_at=time.time() if status == 'succeeded' else None)
            if status == 'failed': m.users.refund_job('sample_user', jid)
        m.jobs.create('eeeeee000001', openid='other_user', status='succeeded')
        first = self.client.get('/api/my/jobs?status=succeeded&limit=1', headers=self.headers).json()
        self.assertEqual(first['total'], 2); self.assertEqual(first['all_total'], 5)
        self.assertEqual(first['status_counts'], {'processing': 1, 'succeeded': 2, 'failed': 2})
        self.assertEqual(first['jobs'][0]['id'], 'aabbcc000001'); self.assertTrue(first['has_more'])
        second = self.client.get('/api/my/jobs?status=succeeded&limit=1&offset=1', headers=self.headers).json()
        self.assertEqual(second['jobs'][0]['id'], 'aabbcc000004'); self.assertFalse(second['has_more'])
        failed = self.client.get('/api/my/jobs?status=failed', headers=self.headers).json()['jobs'][0]
        self.assertEqual((failed['charged_amount'], failed['refunded_amount'], failed['settlement']), (10, 10, 'failed'))
        self.assertIn('expires_at', first['jobs'][0]); self.assertIn('completed_at', first['jobs'][0])
        self.assertEqual(self.client.get('/api/my/jobs?status=invalid', headers=self.headers).status_code, 400)

    def test_experience_recipe_snapshot_original_not_comparison_and_owner(self):
        recipe = photo_recipe('fine', None, {}, 'original', '保留肤色', '')
        jid = self.job(recipe=recipe, comparison_cos='comparisons/fixture.jpg')
        with patch.object(m.settings, 'cos_ready', return_value=True), patch.object(m, 'cos_presign',
                  side_effect=lambda settings, verb, key, **kw: 'https://fixture.invalid/' + key):
            m.jobs.update(jid, orig_cos='origins/full.jpg')
            data = self.client.get('/api/jobs/' + jid + '/recipe', headers=self.headers).json()
        self.assertTrue(data['recipe_complete']); self.assertTrue(data['complete'])
        self.assertEqual(data['orig_url'], 'https://fixture.invalid/origins/full.jpg')
        self.assertEqual(data['custom_prompt'], '保留肤色'); self.assertEqual(data['text_fields'], {})
        self.assertEqual(self.client.get('/api/jobs/' + jid + '/recipe', headers=self.other_headers).status_code, 404)
        m.jobs.update(jid, completed_at=time.time() - m.JOB_TTL_SECONDS - 1)
        self.assertEqual(self.client.get('/api/jobs/' + jid + '/recipe', headers=self.headers).status_code, 410)

    def test_experience_old_recipe_explicitly_incomplete_never_uses_current_prompt(self):
        jid = self.job(input_mode='text')
        data = self.client.get('/api/jobs/' + jid + '/recipe', headers=self.headers).json()
        self.assertFalse(data['recipe_complete']); self.assertIn('prompt', data['missing_fields'])
        self.assertIsNone(data['prompt']); self.assertIsNone(data['orig_url'])
        m.jobs.update(jid, status='processing')
        self.assertEqual(self.client.get('/api/jobs/' + jid + '/recipe', headers=self.headers).status_code, 409)

    def test_experience_text_price_change_and_free_mode_checked_before_debit(self):
        self.text_enabled()
        with patch.object(m.settings, 'cos_ready', return_value=True), patch.object(m.pool, 'submit'):
            response = self.client.post('/api/text-generation', headers=self.headers,
                json={'prompt': '森林', 'expected_price': 30, 'client_request_id': 'quote_fixture'})
            self.assertEqual(response.status_code, 409); self.assertEqual(m.users.get_balance('sample_user'), 100)
            m.settings.update({'free_mode': True})
            self.assertEqual(self.client.post('/api/text-generation', headers=self.headers,
                json={'prompt': '森林', 'expected_price': 40}).status_code, 409)
            accepted = self.client.post('/api/text-generation', headers=self.headers,
                json={'prompt': '森林', 'expected_price': 0}).json()
            self.assertTrue(accepted['free_mode']); self.assertEqual(m.users.get_balance('sample_user'), 100)
            m.jobs.update(accepted['job_id'], status='succeeded', completed_at=time.time())
            data = self.client.get('/api/jobs/' + accepted['job_id'] + '/recipe', headers=self.headers).json()
            self.assertTrue(data['complete']); self.assertEqual(data['prompt'], '森林')

    def test_experience_text_duplicate_and_lookup_same_job_bypass_new_quota(self):
        self.text_enabled()
        payload = {'prompt': '森林', 'expected_price': 40, 'client_request_id': 'text_fixture_01'}
        with patch.object(m.settings, 'cos_ready', return_value=True), patch.object(m.pool, 'submit') as submit:
            first = self.client.post('/api/text-generation', headers=self.headers, json=payload)
            self.assertEqual(first.status_code, 200, first.text)
            m.settings.update({'quota': {'daily': 1, 'per_minute': 1}})
            repeated = self.client.post('/api/text-generation', headers=self.headers, json=payload)
            self.assertEqual(repeated.status_code, 200, repeated.text)
            self.assertEqual(first.json()['job_id'], repeated.json()['job_id']); submit.assert_called_once()
            conflict = self.client.post('/api/text-generation', headers=self.headers, json={**payload, 'prompt': '新内容'})
            self.assertEqual(conflict.status_code, 409)
        found = self.client.get('/api/me/submissions/text_fixture_01', headers=self.headers).json()
        self.assertEqual(found['state'], 'accepted'); self.assertEqual(found['job_id'], first.json()['job_id'])
        other = self.client.get('/api/me/submissions/text_fixture_01', headers=self.other_headers)
        self.assertEqual(other.status_code, 404); self.assertEqual(other.json()['state'], 'not_found')
        self.assertEqual(m.users.get_balance('sample_user'), 60)

    def test_experience_text_quote_changes_during_moderation_do_not_charge(self):
        self.text_enabled()
        def moderation(*args):
            m.settings.update({'text_generation': {'price': 55}})
            return None
        with patch.object(m.settings, 'cos_ready', return_value=True), patch.object(m.pool, 'submit'), \
             patch.object(m, '_moderate_text_or_reject', side_effect=moderation):
            response = self.client.post('/api/text-generation', headers=self.headers,
                json={'prompt': '森林', 'expected_price': 40})
        self.assertEqual(response.status_code, 409); self.assertEqual(m.users.get_balance('sample_user'), 100)

    def test_experience_idempotency_concurrent_claim_admits_and_charges_once(self):
        entered = threading.Event(); release = threading.Event(); calls = []
        user = m.users.get_user('sample_user')
        def admit(jid):
            calls.append(jid); entered.set(); release.wait(5)
            balance = m.users.reserve_job('sample_user', jid, 40, {})
            m.jobs.create(jid, openid='sample_user', cloud_pipeline=True)
            m.users.confirm_job(jid, 'fixture')
            return {'code': 0, 'job_id': jid, 'balance': balance}
        with ThreadPoolExecutor(max_workers=12) as pool:
            first = pool.submit(idempotent_submit, m, user, 'concurrent_fixture', {'prompt': '森林'}, 'text', admit)
            self.assertTrue(entered.wait(5))
            repeated = list(pool.map(lambda _: idempotent_submit(m, user, 'concurrent_fixture',
                {'prompt': '森林'}, 'text', admit), range(10)))
            self.assertTrue(all(r.status_code == 202 for r in repeated))
            release.set(); response = first.result()
        self.assertEqual(len(calls), 1); self.assertEqual(m.users.get_balance('sample_user'), 60)
        self.assertEqual(store_for(m.users).submission('sample_user', 'concurrent_fixture')['job_id'], response['job_id'])

    def test_experience_pending_restart_remains_uncertain_never_replays(self):
        store = store_for(m.users)
        row, _ = store.claim('sample_user', 'restart_fixture', {'prompt': '森林'}, 'text')
        reopened = UserStore(str(self.d))
        try:
            restarted = ExperienceStore(reopened)
            same, new = restarted.claim('sample_user', 'restart_fixture', {'prompt': '森林'}, 'text')
            self.assertFalse(new); self.assertEqual(same['state'], 'uncertain')
            runtime = SimpleNamespace(users=reopened, jobs=m.jobs)
            data = submission_view(runtime, 'sample_user', same)
            self.assertEqual(data['state'], 'uncertain'); self.assertIsNone(data['job_id'])
            self.assertEqual(reopened.get_balance('sample_user'), 100)
        finally: reopened.close()

    def test_experience_lost_receipt_recovers_committed_job_and_debit(self):
        user = m.users.get_user('sample_user')
        def interrupted(jid):
            m.users.reserve_job('sample_user', jid, 40, {})
            m.jobs.create(jid, openid='sample_user', cloud_pipeline=True, quality='fine', charged_amount=40)
            m.users.confirm_job(jid, 'fixture')
            raise TimeoutError('response lost')
        with self.assertRaises(TimeoutError):
            idempotent_submit(m, user, 'lost_receipt_fixture', {'quality': 'fine'}, 'photo', interrupted)
        row = store_for(m.users).submission('sample_user', 'lost_receipt_fixture')
        m.jobs._conn.close(); m.jobs = m.JobStore(2592000, 5000, db_path=str(self.d / 'jobs.db'))
        response = idempotent_submit(m, user, 'lost_receipt_fixture', {'quality': 'fine'}, 'photo',
                                     lambda jid: self.fail('MUST_NOT_REPLAY'))
        self.assertEqual(response['state'], 'accepted'); self.assertEqual(response['job_id'], row['job_id'])
        self.assertEqual(m.users.get_balance('sample_user'), 60)

    def test_experience_ambiguous_without_job_keeps_key_and_never_false_not_found(self):
        user = m.users.get_user('sample_user')
        def interrupted(jid): raise RuntimeError('admission interrupted')
        with self.assertRaises(RuntimeError):
            idempotent_submit(m, user, 'unknown_fixture', {'prompt': '森林'}, 'text', interrupted)
        response = self.client.get('/api/me/submissions/unknown_fixture', headers=self.headers)
        self.assertEqual(response.status_code, 202); self.assertEqual(response.json()['state'], 'uncertain')
        repeat = idempotent_submit(m, user, 'unknown_fixture', {'prompt': '森林'}, 'text',
                                   lambda jid: self.fail('MUST_NOT_REPLAY'))
        self.assertEqual(repeat.status_code, 202)

    def test_experience_photo_by_upload_duplicate_new_upload_id_same_job(self):
        m.settings.update({'cloud_pipeline': {'enabled': True}, 'providers': {'worldcodes': {
            'enabled': True, 'base_url': 'https://fixture.invalid', 'api_key': 'fixture'}}})
        with patch.object(m.settings, 'cos_ready', return_value=True), patch.object(m.cloud, 'ready', return_value=True):
            m._uploads['upload_fixture'] = {'openid': 'sample_user', 'key': 'incoming/a.jpg', 'ext': '.jpg',
                                             'created_at': time.time(), 'filename': 'a.jpg'}
            payload = {'upload_id': 'upload_fixture', 'quality': 'fine', 'expected_price': 40,
                       'client_request_id': 'photo_fixture_01'}
            first = self.client.post('/api/rescue/by-upload', headers=self.headers, json=payload)
            self.assertEqual(first.status_code, 200, first.text)
            repeated = self.client.post('/api/rescue/by-upload', headers=self.headers,
                json={**payload, 'upload_id': 'new_transport_fixture'})
            self.assertEqual(repeated.status_code, 200, repeated.text)
            self.assertEqual(first.json()['job_id'], repeated.json()['job_id'])
        self.assertEqual(m.users.get_balance('sample_user'), 60)
        self.assertIn('recipe', m.jobs.get(first.json()['job_id']))

    def test_experience_photo_price_change_rejects_before_consuming_upload(self):
        m._uploads['quote_upload'] = {'openid': 'sample_user', 'key': 'incoming/a.jpg', 'ext': '.jpg',
                                     'created_at': time.time(), 'filename': 'a.jpg'}
        response = self.client.post('/api/rescue/by-upload', headers=self.headers,
            json={'upload_id': 'quote_upload', 'quality': 'fine', 'expected_price': 0})
        self.assertEqual(response.status_code, 409); self.assertIsNotNone(m._uploads.get('quote_upload'))
        self.assertEqual(m.users.get_balance('sample_user'), 100)

    def test_experience_community_liked_category_filter_before_pagination(self):
        editorial = CommunityStore(m.settings)
        posts = [editorial.create({'title': 'fixture ' + str(i), 'result_url': 'https://fixture.invalid/p.jpg',
                  'status': 'published', 'category': category, 'sort': i})
                 for i, category in enumerate(('film', 'portrait', 'portrait', 'portrait'))]
        community = community_for(m.users)
        for p in posts[2:]: community.set_like(p['id'], 'sample_user', True)
        first = self.client.get('/api/community?liked_only=true&category=portrait&limit=1', headers=self.headers)
        self.assertEqual(first.status_code, 200, first.text); data = first.json()
        self.assertEqual(data['total'], 2); self.assertTrue(data['has_more']); self.assertTrue(data['items'][0]['liked'])
        second = self.client.get('/api/community?liked_only=true&category=portrait&limit=1&offset=1', headers=self.headers).json()
        self.assertEqual(second['total'], 2); self.assertFalse(second['has_more'])
        self.assertNotEqual(data['items'][0]['id'], second['items'][0]['id'])
        self.assertEqual(self.client.get('/api/community?liked_only=true', headers=self.other_headers).json()['total'], 0)
        self.assertEqual(self.client.get('/api/community?liked_only=true').status_code, 401)
        self.assertEqual(self.client.get('/api/community?category=invalid').status_code, 400)


if __name__ == '__main__':
    suite = unittest.TestSuite(ExperienceTests(name) for name in ExperienceTests.__dict__ if name.startswith('test_experience_'))
    names = [case._testMethodName for case in suite]
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    failed = {case._testMethodName for case, _ in result.failures + result.errors}
    (OUTPUT / 'experience_backend_results.json').write_text(json.dumps({
        'cases': [{'case': name, 'passed': name not in failed} for name in names]}, indent=2), encoding='utf-8')
    for connection in initial_connections:
        try: connection.close()
        except Exception: pass
    print('EXPERIENCE_BACKEND_SUMMARY total=%d passed=%d failed=%d' % (result.testsRun, result.testsRun - len(failed), len(failed)))
    raise SystemExit(0 if result.wasSuccessful() else 1)
