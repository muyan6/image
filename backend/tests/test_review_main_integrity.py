"""Review regressions for storage placement and the paid/delivered boundaries."""
import json
import contextlib
import io
import os
from pathlib import Path
import re
import runpy
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_workflow import WorkflowTests, m, OUTPUT, initial_connections


class MainIntegrityTests(WorkflowTests):
    def register(self):
        source = Path(m.UPLOAD_DIR) / (self._testMethodName + '.jpg')
        source.write_bytes(self.image())
        with patch.object(m.pool, 'submit'):
            return m._register_job('sample_user', 'light', 'clear', str(source), '.jpg')['job_id']

    def test_configured_data_directory_contains_default_job_database(self):
        directory = self.d / 'configured-data'
        with patch.object(m, 'DATA_DIR', str(directory)), patch.object(m, 'BASE_DIR', str(self.d / 'fresh-checkout')):
            store = m.JobStore(2592000, 5000)
        try:
            self.assertEqual(Path(store._db_path), directory / 'jobs.db')
        finally:
            store._conn.close()

    def test_legacy_job_database_is_kept_without_implicit_history_migration(self):
        checkout = self.d / 'legacy-checkout'
        legacy = checkout / 'data/jobs.db'
        old = m.JobStore(2592000, 5000, db_path=str(legacy))
        old.create('abc987', openid='sample_user', status='succeeded')
        old._conn.close()
        with patch.object(m, 'DATA_DIR', str(self.d / 'configured-data')), patch.object(m, 'BASE_DIR', str(checkout)):
            restored = m.JobStore(2592000, 5000)
        try:
            self.assertEqual(Path(restored._db_path), legacy)
            self.assertEqual(restored.get('abc987')['status'], 'succeeded')
        finally:
            restored._conn.close()

    def test_published_local_result_survives_post_delivery_retention_failure(self):
        jid = self.register()
        schedule = m.cleanup.schedule

        def transient_failure(kind, key, due):
            if key == 'result_' + jid + '.jpg' and m.jobs.get(jid)['status'] == 'succeeded':
                raise RuntimeError('fixture post-delivery retention error')
            return schedule(kind, key, due)

        provider=SimpleNamespace(configured=True,enhance=lambda src,dst,**kw:Path(dst).write_bytes(self.image()))
        with patch.object(m.cleanup, 'schedule', side_effect=transient_failure),patch.object(m,'_get_client',return_value=provider):
            m._run_pipeline(jid, 'light', 'clear')
        job = m.jobs.get(jid)
        self.assertEqual(job['status'], 'succeeded')
        self.assertTrue((Path(m.UPLOAD_DIR) / job['result_file']).is_file())
        self.assertEqual(m.users.get_balance('sample_user'), 60)
        self.assertIsNotNone(m._job_media_url(job, 'result'))

    def test_uncertain_paid_request_stops_provider_fallback(self):
        fallback='gw_'+'b'*32
        m.settings.update({'chain': ['worldcodes',fallback],
                           'providers': {'worldcodes': {'enabled': True},fallback:{'name':'备用夹具','enabled':True,
                               'base_url':'https://fallback.invalid','api_key':'fallback-fixture','model_light':'fixture','model_fine':'fixture'}}})
        jid = self.register()
        calls = []

        def client(name, quality):
            def enhance(source, output, **kwargs):
                calls.append(name)
                if name == 'worldcodes':
                    error = RuntimeError('fixture accepted POST with lost response')
                    error.uncertain = True
                    raise error
                Path(output).write_bytes(self.image())
            return SimpleNamespace(configured=True, enhance=enhance)

        with patch.object(m, '_get_client', side_effect=client):
            m._run_pipeline(jid, 'light', 'clear')
        self.assertEqual(calls, ['worldcodes'])
        self.assertEqual(m.jobs.get(jid)['status'], 'failed')
        self.assertEqual(m.users.get_balance('sample_user'), 100)

    def test_review_runner_preserves_nonzero_process_status(self):
        root = Path(os.environ.get('REVIEW_ROOT', Path(__file__).resolve().parents[2]))
        runner = root / 'backend/tests/run_review.py'
        output = self.d / 'runner-reports'
        output.mkdir()
        for name in set(re.findall(r'[A-Za-z_]+_results\.json', runner.read_text(encoding='utf-8'))):
            value = [] if name in ('backend_results.json', 'frontend_results.json') else {'cases': []}
            (output / name).write_text(json.dumps(value), encoding='utf-8')
        failure = SimpleNamespace(returncode=1, stdout='FIXTURE_PROCESS_FAILURE\n', stderr='')
        argv = [str(runner), '--source-root', str(root), '--output', str(output)]
        with patch.object(sys, 'argv', argv), patch('subprocess.run', return_value=failure), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as stopped:
                runpy.run_path(str(runner), run_name='__main__')
        self.assertEqual(stopped.exception.code, 1)

    def test_invalid_paid_result_does_not_start_another_provider(self):
        fallback='gw_'+'b'*32
        m.settings.update({'chain': ['worldcodes',fallback],
                           'providers': {'worldcodes': {'enabled': True},fallback:{'name':'备用夹具','enabled':True,
                               'base_url':'https://fallback.invalid','api_key':'fallback-fixture','model_light':'fixture','model_fine':'fixture'}}})
        jid = self.register()
        calls = []

        def client(name, quality):
            def enhance(source, output, **kwargs):
                calls.append(name)
                Path(output).write_bytes(b'fixture corrupt paid image' if name == 'worldcodes' else self.image())
            return SimpleNamespace(configured=True, enhance=enhance)

        with patch.object(m, '_get_client', side_effect=client):
            m._run_pipeline(jid, 'light', 'clear')
        self.assertEqual(calls, ['worldcodes'])
        self.assertEqual(m.jobs.get(jid)['status'], 'failed')
        self.assertEqual(m.users.get_balance('sample_user'), 100)


if __name__ == '__main__':
    suite = unittest.TestSuite(MainIntegrityTests(n) for n in MainIntegrityTests.__dict__ if n.startswith('test_'))
    names = [t._testMethodName for t in suite]
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    failed = {t._testMethodName for t, _ in result.failures + result.errors}
    (OUTPUT / 'review_main_integrity_results.json').write_text(json.dumps({
        'cases': [{'case': n, 'passed': n not in failed} for n in names]
    }, indent=2), encoding='utf-8')
    for connection in initial_connections:
        try:
            connection.close()
        except Exception:
            pass
    print('MAIN_INTEGRITY_SUMMARY total=%d passed=%d failed=%d' % (result.testsRun, result.testsRun - len(failed), len(failed)))
    raise SystemExit(0 if result.wasSuccessful() else 1)
