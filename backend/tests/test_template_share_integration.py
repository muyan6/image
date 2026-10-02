"""Actual main admission/admin/catalog integration; immutable COS and AI boundaries are offline.

Reuse the approved sharing lifecycle, but retain main.app rather than the parent's
standalone sharing router. Generic admin factories are rebound to this test's
isolated stores because WorkflowTests replaces the module globals after import.
No endpoint, billing hook, catalog lookup or reward eligibility is mocked.
"""
import copy
import json
from pathlib import Path
import time
import unittest
import uuid
from contextlib import ExitStack
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_template_shares import TemplateShareTests, m, OUTPUT, initial_connections
from admin_api import make_admin_router
from gateway_async import AsyncImages
import cloud_pipeline as cp
from template_share_rewards import reward_summary


class TemplateShareIntegrationTests(TemplateShareTests):
    def setUp(self):
        super().setUp()
        def flattened(routes):
            for route in routes:
                nested = getattr(route, 'original_router', None)
                if nested is not None:
                    yield from flattened(nested.routes)
                else:
                    yield route
        current = make_admin_router(settings=m.settings, announcements=m.announcements,
            templates=m.templates, jobs=m.jobs, users=m.users, health_fn=m.health, stats_fn=lambda: {})
        app = FastAPI()
        app.router.routes = [r for r in flattened(m.app.router.routes)
                             if not getattr(r, 'path', '').startswith('/admin/api/')]
        app.include_router(current)
        self.admin_client = TestClient(app)
        self.admin_client.cookies.update(self.admin.cookies)
        self.client.close()
        self.main_client = TestClient(m.app)
        self.main_client.cookies.update(self.admin.cookies)
        self.client = self.main_client
        self.consumer_headers = {'Authorization': 'Bearer ' + m.user_token('other_user')}

    def tearDown(self):
        self.admin_client.close()
        super().tearDown()

    def approve_actual(self):
        pending = self.submit(self.ready_share())
        response = self.review(pending)
        self.assertEqual(response.status_code, 200, response.text)
        row = response.json()['submission']
        public = self.main_client.get('/api/templates').json()['items']
        item = next(t for t in public if t['id'] == row['published_template_id'])
        self.assertEqual(item['source'], 'user')
        self.assertEqual(item['author_name'], '模板作者')
        self.assertNotIn('author_openid', item)
        self.assertNotIn('prompt', item)
        template = m.templates.get_template(item['id'], enabled_only=True)
        self.assertIsNotNone(template)
        self.assertEqual(template['author_openid'], 'sample_user')
        return row, template

    def upload_source(self):
        upload_id = 'integration_' + uuid.uuid4().hex[:16]
        m._uploads[upload_id] = {**self.source, 'key': 'incoming/' + upload_id + '.jpg',
            'openid': 'other_user', 'created_at': time.time()}
        return upload_id

    def admit_actual(self, template_id):
        upload_id = self.upload_source()
        with patch.object(m, '_moderate_text_or_reject', return_value=None):
            response = self.main_client.post('/api/rescue/by-upload', headers=self.consumer_headers,
                json={'upload_id': upload_id, 'quality': 'light', 'template_id': template_id,
                      'expected_price': 40, 'client_request_id': 'wc_it_' + uuid.uuid4().hex})
        self.assertEqual(response.status_code, 200, response.text)
        job = m.jobs.get(response.json()['job_id'])
        self.assertTrue(job['cloud_pipeline'])
        self.assertEqual(job['charged_amount'], 40)
        self.assertEqual(m.users.get_balance('other_user'), 60)
        return job

    def finish_cloud_actual(self, job):
        """CloudTests' no-host path with shared cos module mocks scoped to the worker."""
        with ExitStack() as stack:
            for name, value in [('object_metadata', {'size': 1000, 'content_type': 'image/jpeg'}),
                                ('image_info', {'width': 1536, 'height': 1024}),
                                ('copy_object', None), ('process_image', {'size': 1000}),
                                ('trigger_mirror', None)]:
                stack.enter_context(patch.object(cp.cos, name, return_value=value))
            for name in ('cos_get', 'cos_put', '_validate_image'):
                stack.enter_context(patch.object(m, name, side_effect=AssertionError('NO_HOST_IMAGE_IO')))
            stack.enter_context(patch.object(AsyncImages, 'submit', return_value='imgtask_integration'))
            self.step(job['id'], 'prepare')
            self.step(job['id'], 'submit')
            result = self.completed(job['id'])
        self.assertEqual(result['status'], 'succeeded', result.get('error'))
        self.assertEqual(result['processing_mode'], 'cloud_only')
        self.assertEqual(result['orig_file'], '')
        self.assertEqual(result['result_file'], '')
        return result

    def test_integration_create_cover_consent_approve_catalog_cloud_delivery_awards_author_only(self):
        row, template = self.approve_actual()
        self.assertEqual(m.users.get_balance('sample_user'), 100)
        job = self.admit_actual(template['id'])
        self.assertEqual(job['template_snapshot']['source_revision'], template['source_revision'])
        self.finish_cloud_actual(job)
        self.assertEqual(m.users.get_balance('sample_user'), 140)
        self.assertEqual(m.users.get_balance('other_user'), 60)
        self.assertEqual(reward_summary(m.users, 'sample_user')['reward_credited'], 40)
        self.assertEqual(reward_summary(m.users, 'other_user')['reward_credited'], 0)
        m.users.complete_charge(job['id'])
        self.assertEqual(m.users.get_balance('sample_user'), 140)
        public = next(t for t in self.main_client.get('/api/templates').json()['items'] if t['id'] == template['id'])
        self.assertEqual(public['usage_count'], 1)

    def test_integration_real_admission_freezes_reviewed_source_and_reward_policy(self):
        row, template = self.approve_actual()
        job = self.admit_actual(template['id'])
        snapshot = copy.deepcopy(job['template_snapshot'])
        self.assertEqual(snapshot['share_reward_policy']['reward_amount'], 40)
        m.settings.update({'template_sharing': {'reward_amount': 90, 'reward_mode': 'all_success'}})
        response = self.review(row, patch={'prompt': '审核后的新版水彩'})
        self.assertEqual(response.status_code, 200, response.text)
        current = m.templates.get_template(template['id'], enabled_only=True)
        self.assertGreater(current['source_revision'], snapshot['source_revision'])
        self.assertEqual(m.jobs.get(job['id'])['template_snapshot'], snapshot)
        self.finish_cloud_actual(job)
        self.assertEqual(m.users.get_balance('sample_user'), 140)
        cur = m.users._conn.execute('SELECT snapshot FROM template_reward_jobs WHERE job_id=?', (job['id'],))
        self.assertEqual(json.loads(cur.fetchone()[0])['share_reward_policy']['reward_mode'], 'first_user')

    def test_integration_reviewed_catalog_local_registration_and_real_success_hook(self):
        m.settings.update({'providers':{'worldcodes':{'request_mode':'sync'}}})
        row, template = self.approve_actual()
        source = self.d / 'local-input.jpg'; source.write_bytes(self.image())
        image = self.image()
        class Provider:
            configured = True
            def enhance(self, source, output, **kwargs): Path(output).write_bytes(image)
        with patch.object(m.pool, 'submit'), patch.object(m, '_get_client', return_value=Provider()), \
             patch.object(m, 'cos_put', return_value=None):
            result = m._register_job('other_user', 'light', '', str(source), '.jpg', template=template, expected_price=40)
        self.assertEqual(m.users.get_balance('other_user'), 60)
        self.assertEqual(m.jobs.get(result['job_id'])['template_snapshot']['source_revision'], template['source_revision'])
        with patch.object(m.settings, 'chain', return_value=['worldcodes']), \
             patch.object(m.settings, 'provider_enabled', return_value=True), \
             patch.object(m, '_get_client', return_value=Provider()), \
             patch.object(m, '_moderate_or_reject', return_value=None), \
             patch.object(m, 'cos_head', return_value=True), \
             patch.object(m, 'cos_put', return_value=None):
            m._run_pipeline(result['job_id'], 'light', '', template=template)
        self.assertEqual(m.jobs.get(result['job_id'])['status'], 'succeeded',m.jobs.get(result['job_id']).get('error'))
        self.assertEqual(m.users.get_balance('sample_user'), 140)
        self.assertEqual(m.users.get_balance('other_user'), 60)

    def test_integration_generic_admin_mutations_are_409_without_sqlite_or_catalog_fork(self):
        row, template = self.approve_actual()
        before = copy.deepcopy(self.store.get(row['id']))
        tid = template['id']; headers = self.admin_headers
        requests = [self.admin_client.put('/admin/api/templates/' + tid, headers=headers, json={'prompt': '绕过审核'}),
                    self.admin_client.delete('/admin/api/templates/' + tid, headers=headers),
                    self.admin_client.post('/admin/api/templates/' + tid + '/covers?slot=0', headers=headers,
                        files={'file': ('cover.jpg', self.image(), 'image/jpeg')}),
                    self.admin_client.delete('/admin/api/templates/' + tid + '/covers/0', headers=headers)]
        for response in requests:
            self.assertEqual(response.status_code, 409, response.text)
            self.assertIn('投稿审核', response.json()['detail'])
        self.assertEqual(self.store.get(row['id']), before)
        self.assertEqual(m.templates.get_template(tid), template)

    def test_integration_withdraw_disables_real_template_lookup_before_rescue_charge_or_upload_pop(self):
        row, template = self.approve_actual()
        response = self.main_client.post('/api/template-shares/' + row['id'] + '/withdraw',
            headers=self.headers, json={'revision': row['revision']})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(m.templates.get_template(template['id'], enabled_only=True))
        self.assertNotIn(template['id'], [t['id'] for t in self.main_client.get('/api/templates').json()['items']])
        upload_id = self.upload_source()
        response = self.main_client.post('/api/rescue/by-upload', headers=self.consumer_headers,
            json={'upload_id': upload_id, 'quality': 'light', 'template_id': template['id'], 'expected_price': 40})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(m.users.get_balance('other_user'), 100)
        self.assertIsNotNone(m._uploads.get(upload_id))
        self.assertEqual(reward_summary(m.users, 'sample_user')['reward_credited'], 0)

    def test_integration_official_template_legacy_admin_create_and_update_still_work(self):
        response = self.admin_client.post('/admin/api/templates', headers=self.admin_headers,
            json={'name': '官方新模板', 'group_id': 'anime', 'prompt': '清晰水彩', 'engine': 'light', 'price': 40})
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json(); template = data.get('item', data)
        tid = template['id']
        response = self.admin_client.put('/admin/api/templates/' + tid, headers=self.admin_headers, json={'subtitle': '原路径仍可维护'})
        self.assertEqual(response.status_code, 200, response.text)
        live = m.templates.get_template(tid, enabled_only=True)
        self.assertEqual(live['subtitle'], '原路径仍可维护')
        public = next(t for t in self.main_client.get('/api/templates').json()['items'] if t['id'] == tid)
        self.assertEqual(public['source'], 'official')

    def test_integration_pending_share_cannot_be_selected_through_real_rescue_endpoint(self):
        pending = self.submit(self.ready_share())
        upload_id = self.upload_source()
        response = self.main_client.post('/api/rescue/by-upload', headers=self.consumer_headers,
            json={'upload_id': upload_id, 'template_id': pending['published_template_id'], 'quality': 'light'})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(m.users.get_balance('other_user'), 100)
        self.assertEqual(m.users.get_balance('sample_user'), 100)


if __name__ == '__main__':
    names = [n for n in TemplateShareIntegrationTests.__dict__ if n.startswith('test_integration_')]
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(TemplateShareIntegrationTests(n) for n in names))
    failed = {test._testMethodName for test, _ in result.failures + result.errors}
    (OUTPUT / 'template_share_integration_results.json').write_text(json.dumps({
        'cases': [{'case': name, 'passed': name not in failed} for name in names],
        'summary': {'total': result.testsRun, 'passed': result.testsRun - len(failed), 'failed': len(failed)}},
        ensure_ascii=False, indent=2), encoding='utf-8')
    for conn in initial_connections:
        try: conn.close()
        except Exception: pass
    print('TEMPLATE_SHARE_INTEGRATION_SUMMARY total=%d passed=%d failed=%d' %
          (result.testsRun, result.testsRun - len(failed), len(failed)))
    raise SystemExit(0 if result.wasSuccessful() else 1)
