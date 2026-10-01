"""Offline regressions for delivered debits, accepted sources and history totals."""
import json
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from test_cloud_pipeline import CloudTests, m, cp, OUTPUT, initial_connections


class FullReviewMainTests(CloudTests):
    def test_deleted_success_reconciles_as_delivered_after_restart(self):
        jid = self.generating()
        update = m.jobs.update

        def delivered_then_delete(job_id, **fields):
            result = update(job_id, **fields)
            if fields.get('status') == 'succeeded':
                response = self.client.delete('/api/my/jobs/' + job_id, headers=self.headers)
                self.assertEqual(response.status_code, 200, response.text)
            return result

        with patch.object(m.jobs, 'update', side_effect=delivered_then_delete), \
                patch.object(m.cleanup, 'delete_cos_now', return_value=3):
            self.completed(jid)
        self.assertTrue(m.jobs.get(jid)['deleted_at'])
        db = m.jobs._db_path
        m.jobs._conn.close()
        m.jobs = m.JobStore(m.JOB_TTL_SECONDS, 5000, db_path=db)
        self.assertEqual(m.users.get_balance('sample_user'), 60)
        self.assertEqual(m.users._conn.execute(
            'SELECT state,refunded FROM job_charges WHERE job_id=?', (jid,)).fetchone(), ('succeeded', 0))

    def test_restart_settles_success_tombstone_even_before_delete_settlement(self):
        jid = self.new()
        m.jobs.update(jid, status='succeeded', deleted_at=time.time(), completed_at=time.time())
        db = m.jobs._db_path
        m.jobs._conn.close()
        m.jobs = m.JobStore(m.JOB_TTL_SECONDS, 5000, db_path=db)
        self.assertEqual(m.users.get_balance('sample_user'), 60)
        self.assertEqual(m.users._conn.execute(
            'SELECT state,refunded FROM job_charges WHERE job_id=?', (jid,)).fetchone(), ('succeeded', 0))

    def test_late_valid_upload_survives_old_permit_cleanup_deadline(self):
        now = time.time()
        key = 'uploads/owned/late.jpg'
        permit = 'fixture-late-permit'
        source = {**self.source, 'key': key, 'created_at': now - 3500, 'byte_size': 1000}
        m._uploads[permit] = source
        m.cleanup.protect_upload_until(key, now + 100)
        m.cleanup.schedule('cos', key, now + 100)
        media = {key: 1000}

        def metadata(settings, target):
            if target not in media:
                raise cp.cos.CosError('not found', status=404)
            return {'size': media[target], 'content_type': 'image/jpeg'}

        def copy(settings, src, dst, **kwargs):
            metadata(settings, src)
            media[dst] = media[src]

        with patch.object(cp.cos, 'object_metadata', side_effect=metadata), \
                patch.object(cp.cos, 'copy_object', side_effect=copy), \
                patch('cleanup_store.delete_object', side_effect=lambda settings, target, **kwargs: media.pop(target, None)):
            response = self.client.post('/api/rescue/by-upload', headers=self.headers,
                                        json={'upload_id': permit, 'quality': 'light', 'expected_price': 40})
            self.assertEqual(response.status_code, 200, response.text)
            jid = response.json()['job_id']
            with patch('cleanup_store.time.time', return_value=now + 101):
                m.cleanup.run(m.settings)
            self.assertIn(key, media)
            m.jobs.update(jid, cloud_phase='prepare')
            self.cloud.step(jid)
            self.assertEqual(m.jobs.get(jid)['cloud_phase'], 'submit')

    def test_source_retention_failure_is_not_a_paid_accepted_job(self):
        with patch.object(m.cleanup, 'retain_until', side_effect=OSError('fixture retention failed')):
            with self.assertRaises(OSError):
                self.new()
        self.assertEqual(m.users.get_balance('sample_user'), 100)
        self.assertEqual(len(m.jobs.pending_cloud()), 0)

    def test_my_jobs_totals_cover_more_than_one_page_and_exclude_deleted(self):
        for number in range(103):
            jid = format(number, '012x')
            m.jobs.create(jid, openid='sample_user', status='succeeded' if number < 101 else 'processing')
        m.jobs.delete_for_openid('000000000064', 'sample_user')
        response = self.client.get('/api/my/jobs?limit=100', headers=self.headers)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data.get('total'), 102)
        self.assertEqual(data.get('processing_count'), 2)
        self.assertEqual(len(data['jobs']), 100)
        self.assertTrue(data['has_more'])
        other = self.client.get('/api/my/jobs', headers={'Authorization': 'Bearer ' + m.user_token('other_user')}).json()
        self.assertEqual(other.get('total'), 0)

    def test_layout_intermediates_are_owned_cleanup_targets(self):
        job = {'id': 'abcdef123456', 'cloud_pipeline': False,
               'cloud_layout_keys': ['results/owned/layout_1.jpg', 'results/owned/layout_2.jpg']}
        self.assertTrue(set(job['cloud_layout_keys']) <= set(m._job_cos_keys(job)))

    def test_submission_guard_and_dashboard_share_beijing_midnight_on_utc_host(self):
        from datetime import datetime, timezone, timedelta
        zone = timezone(timedelta(hours=8))
        before = datetime(2026, 9, 30, 23, 55, tzinfo=zone).timestamp()
        after = datetime(2026, 10, 1, 0, 5, tzinfo=zone).timestamp()
        m.settings.update({'quota': {'daily': 1, 'per_minute': 1000}, 'free_mode': False})
        with patch.object(m.time, 'time', return_value=before):
            m.users.audit('sample_user', 'submitted', 'job=fixture previous Beijing day')
            m.jobs.create('aabbcc001122', openid='sample_user', status='succeeded')
        with patch.object(m.time, 'time', return_value=after), \
                patch.object(m.time, 'localtime', side_effect=lambda value=None: time.gmtime(after if value is None else value)):
            self.assertIsNone(m._check_quota('sample_user'))
            self.assertEqual(m._admin_stats()['today_total'], 0)


if __name__ == '__main__':
    names = [n for n in FullReviewMainTests.__dict__ if n.startswith('test_')]
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(FullReviewMainTests(n) for n in names))
    failed = {t._testMethodName for t, _ in result.failures + result.errors}
    (OUTPUT / 'full_review_main_results.json').write_text(json.dumps(
        {'cases': [{'case': n, 'passed': n not in failed} for n in names]}, indent=2), encoding='utf-8')
    for connection in initial_connections:
        try:
            connection.close()
        except Exception:
            pass
    print('FULL_REVIEW_MAIN_SUMMARY total=%d passed=%d failed=%d' % (result.testsRun, result.testsRun - len(failed), len(failed)))
    raise SystemExit(0 if result.wasSuccessful() else 1)
