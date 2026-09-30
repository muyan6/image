"""Deterministic latency and paid-submit regressions; isolated stores, no network."""
import base64
import json
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_cloud_pipeline import CloudTests, m, cp, OUTPUT, initial_connections
from gateway_async import AsyncImages
from gateway_ai import OpenAIImagesEnhance, GatewayError
from fal_ai import FalImageEnhance, FalError
from baidu_ai import BaiduImageEnhance, BaiduError
import cloud_import
import gateway_ai
import fal_ai
import baidu_ai
from text_generation import run_text_job


class LatencyTests(CloudTests):
    def immediate_dispatcher(self):
        class Immediate:
            def submit(_, fn, jid):
                fn(jid)
        return patch.object(self.cloud, 'executor', Immediate())

    def importing(self):
        jid = self.new()
        m.jobs.update(jid, cloud_phase='import', vendor_result_key='images/fixture.png',
                      vendor_task_id='imgtask_fixture', provider_completed_at=time.time(),
                      source_width=1536, source_height=1024)
        return jid

    def gateway(self):
        source = Path(m.UPLOAD_DIR, 'latency_input.jpg')
        source.write_bytes(self.image())
        session = Mock()
        client = OpenAIImagesEnhance({'base_url': 'https://fixture.invalid', 'api_key': 'fixture',
                                     'model_light': 'fixture'}, session)
        return client, session, str(source), str(Path(m.UPLOAD_DIR, 'latency_output.jpg'))

    def test_dispatcher_chains_prepare_and_submit_without_extra_tick(self):
        jid = self.new()
        with self.immediate_dispatcher(), patch.object(AsyncImages, 'submit', return_value='imgtask_fixture') as post:
            self.cloud.tick()
        self.assertEqual(m.jobs.get(jid)['cloud_phase'], 'generating')
        post.assert_called_once()
        self.assertNotIn(jid, self.cloud.busy)

    def test_dispatcher_chains_completed_import_and_finalization(self):
        jid = self.importing()
        m.jobs.update(jid, cloud_phase='generating', cloud_next_at=0)
        completed = {'status': 'completed', 'result': {'data': [{'url': 'https://' + cp.MEDIA_HOST + '/images/fixture.png'}]}}
        with self.immediate_dispatcher(), patch.object(AsyncImages, 'poll', return_value=completed):
            self.cloud.tick()
        self.assertEqual(m.jobs.get(jid)['status'], 'succeeded', m.jobs.get(jid).get('error'))

    def test_dispatcher_still_waits_for_future_poll(self):
        jid = self.importing()
        m.jobs.update(jid, cloud_phase='generating', cloud_next_at=time.time() + 30)
        with self.immediate_dispatcher(), patch.object(AsyncImages, 'poll') as poll:
            self.cloud.tick()
        poll.assert_not_called()
        self.assertEqual(m.jobs.get(jid)['status'], 'processing')

    def test_frozen_upload_must_match_registered_size(self):
        self.source['byte_size'] = 999
        jid = self.new()
        self.step(jid, 'prepare')
        job = m.jobs.get(jid)
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(m.users.get_balance('sample_user'), 100)
        cp.cos.process_image.assert_not_called()

    def test_legacy_zero_registered_size_remains_compatible(self):
        self.source['byte_size'] = 0
        jid = self.new()
        self.step(jid, 'prepare')
        self.assertEqual(m.jobs.get(jid)['cloud_phase'], 'submit')

    def test_dispatcher_uncertain_submission_is_not_chained_or_repeated(self):
        from gateway_async import GatewayAsyncError
        jid = self.new()
        with self.immediate_dispatcher(), patch.object(AsyncImages, 'submit',
                side_effect=GatewayAsyncError('pending', uncertain=True)) as submit:
            self.cloud.tick()
            self.cloud.tick()
        submit.assert_called_once()
        self.assertEqual(m.jobs.get(jid)['cloud_phase'], 'unknown')

    def test_dispatcher_idle_start_stop_does_not_spin(self):
        # Stop must wake its event-based dispatcher even without queued work.
        calls = []
        with patch.object(self.cloud, 'tick', side_effect=lambda: calls.append(time.monotonic())):
            self.cloud.start()
            self.cloud.stop()
        self.assertFalse(self.cloud.thread.is_alive())
        self.assertLessEqual(len(calls), 2)

    def test_resumed_output_audit_reuses_durable_metadata(self):
        jid = self.importing()
        m.jobs.update(jid, cloud_phase='finalize', cloud_output_prepared=True,
                      cloud_output_info={'width': 1536, 'height': 1024}, cloud_output_meta={'size': 1000},
                      source_width=1536, source_height=1024)
        cp.cos.object_metadata.reset_mock()
        self.cloud.step(jid)
        self.assertEqual(m.jobs.get(jid)['status'], 'succeeded')
        cp.cos.object_metadata.assert_not_called()
        cp.cos.process_image.assert_not_called()

    def test_first_accepted_backfill_probe_wait_is_half_second(self):
        jid = self.importing()
        now = time.time()
        with patch.object(cp.time, 'time', return_value=now), patch.object(cp.cos, 'object_metadata',
                side_effect=cp.cos.CosError('pending', status=404)):
            self.cloud.step(jid)
        self.assertLessEqual(m.jobs.get(jid)['cloud_next_at'] - now, .5)
        self.assertEqual(m.jobs.get(jid)['import_trigger_attempts'], 1)

    def test_gateway_uncertain_timeout_sends_paid_post_once(self):
        client, session, source, target = self.gateway()
        session.post.side_effect = gateway_ai.requests.Timeout()
        with patch.object(gateway_ai.time, 'sleep'):
            with self.assertRaises(GatewayError) as caught:
                client.enhance(source, target, quality='light')
        self.assertEqual(session.post.call_count, 1)
        self.assertTrue(getattr(caught.exception, 'uncertain', False))

    def test_gateway_download_failure_does_not_regenerate(self):
        client, session, source, target = self.gateway()
        session.post.return_value = SimpleNamespace(status_code=200, headers={},
            json=lambda: {'data': [{'url': 'https://result.invalid/fixture.jpg'}]})
        session.get.side_effect = gateway_ai.requests.ConnectionError()
        with patch.object(gateway_ai.time, 'sleep'):
            with self.assertRaises(GatewayError) as caught:
                client.enhance(source, target, quality='light')
        self.assertEqual(session.post.call_count, 1)
        self.assertTrue(getattr(caught.exception, 'uncertain', False))

    def test_gateway_url_input_skips_local_encoding(self):
        client, session, source, target = self.gateway()
        with patch.object(client, '_maybe_compress', side_effect=AssertionError('LOCAL_ENCODING_NOT_NEEDED')), \
                patch.object(client, '_request_image_by_url', return_value=self.image()):
            client.enhance(source, target, quality='light', image_url='https://cos.invalid/source.jpg')
        self.assertTrue(Path(target).is_file())

    def test_gateway_paid_post_does_not_follow_redirect(self):
        client, session, source, target = self.gateway()
        session.post.return_value = SimpleNamespace(status_code=200, headers={'content-type': 'image/jpeg'}, content=self.image())
        client.enhance(source, target, quality='light')
        self.assertIs(session.post.call_args.kwargs.get('allow_redirects'), False)

    def test_gateway_generated_result_io_failure_preserves_paid_boundary(self):
        client, session, source, target = self.gateway()
        session.post.return_value = SimpleNamespace(status_code=200, headers={'content-type': 'image/jpeg'}, content=self.image())
        with patch.object(client, '_write_output', side_effect=OSError('disk full')):
            with self.assertRaises(GatewayError) as caught:
                client.enhance(source, target, quality='light')
        session.post.assert_called_once()
        self.assertTrue(getattr(caught.exception, 'uncertain', False))

    def test_fal_uncertain_submit_sends_paid_post_once(self):
        session = Mock()
        session.request.side_effect = fal_ai.requests.Timeout()
        client = FalImageEnhance(api_key='fixture', session=session)
        with patch.object(fal_ai.time, 'sleep'):
            with self.assertRaises(FalError) as caught:
                client._submit('fixture/model', {'image_url': 'fixture'})
        self.assertEqual(session.request.call_count, 1)
        self.assertTrue(getattr(caught.exception, 'uncertain', False))

    def test_fal_ambiguous_fine_task_does_not_start_light_task(self):
        client = FalImageEnhance(api_key='fixture')
        error = FalError('pending', code='TIMEOUT')
        error.uncertain = True
        with patch.object(client, 'enhance_fine', side_effect=error), patch.object(client, 'enhance_light') as light:
            with self.assertRaises(FalError):
                client.enhance('fixture.jpg', 'result.jpg')
        light.assert_not_called()

    def test_fal_remote_wait_timeout_is_uncertain(self):
        client = FalImageEnhance(api_key='fixture')
        with self.assertRaises(FalError) as caught:
            client._wait('https://fixture.invalid/status', 'https://fixture.invalid/result', time.time() - 1)
        self.assertTrue(getattr(caught.exception, 'uncertain', False))

    def test_fal_accepted_task_poll_failure_does_not_regenerate(self):
        _, _, source, target = self.gateway()
        client = FalImageEnhance(api_key='fixture')
        with patch.object(client, '_submit', return_value=('https://fixture.invalid/status', 'https://fixture.invalid/result')) as submit, \
                patch.object(client, '_wait', side_effect=FalError('poll unavailable', code='NETWORK')), \
                patch.object(client, 'enhance_light') as light:
            with self.assertRaises(FalError) as caught:
                client.enhance(source, target, quality='fine')
        submit.assert_called_once()
        light.assert_not_called()
        self.assertTrue(getattr(caught.exception, 'uncertain', False))

    def test_fal_unknown_remote_status_does_not_regenerate(self):
        _, _, source, target = self.gateway()
        session = Mock()
        session.request.return_value = SimpleNamespace(status_code=200, json=lambda: {'status': 'UNKNOWN'})
        client = FalImageEnhance(api_key='fixture', session=session)
        with patch.object(client, '_submit', return_value=('https://fixture.invalid/status', 'https://fixture.invalid/result')) as submit, \
                patch.object(client, 'enhance_light') as light:
            with self.assertRaises(FalError) as caught:
                client.enhance(source, target, quality='fine')
        submit.assert_called_once()
        light.assert_not_called()
        self.assertTrue(getattr(caught.exception, 'uncertain', False))

    def test_baidu_accepts_pipeline_style_argument(self):
        _, _, source, target = self.gateway()
        session = Mock()
        session.post.return_value = SimpleNamespace(status_code=200, json=lambda: {
            'image': base64.b64encode(self.image()).decode('ascii')})
        client = BaiduImageEnhance('fixture', 'fixture', session)
        with patch.object(client, 'get_token', return_value='fixture'):
            client.enhance(source, target, quality='light', style='clear')
        self.assertTrue(Path(target).is_file())
        self.assertIs(session.post.call_args.kwargs.get('allow_redirects'), False)

    def test_baidu_uncertain_timeout_sends_paid_post_once(self):
        _, _, source, target = self.gateway()
        session = Mock()
        session.post.side_effect = baidu_ai.requests.Timeout()
        client = BaiduImageEnhance('fixture', 'fixture', session)
        with patch.object(client, 'get_token', return_value='fixture'), patch.object(baidu_ai.time, 'sleep'):
            with self.assertRaises(BaiduError) as caught:
                client.enhance(source, target, quality='light')
        self.assertEqual(session.post.call_count, 1)
        self.assertTrue(getattr(caught.exception, 'uncertain', False))

    def test_baidu_short_token_ttl_is_not_cached_past_expiry(self):
        session = Mock()
        session.post.return_value = SimpleNamespace(status_code=200,
            json=lambda: {'access_token': 'fixture', 'expires_in': 10})
        client = BaiduImageEnhance('fixture', 'fixture', session)
        now = time.time()
        with patch.object(baidu_ai.time, 'time', return_value=now):
            self.assertEqual(client.get_token(), 'fixture')
        self.assertLess(client._token_expires_at, now + 10)

    def test_baidu_successful_paid_response_with_invalid_payload_is_uncertain(self):
        _, _, source, target = self.gateway()
        for payload in ([], {'image': 'not-base64'}):
            session = Mock()
            session.post.return_value = SimpleNamespace(status_code=200, json=lambda: payload)
            client = BaiduImageEnhance('fixture', 'fixture', session)
            with patch.object(client, 'get_token', return_value='fixture'):
                with self.assertRaises(BaiduError) as caught:
                    client.enhance(source, target, quality='light')
            self.assertTrue(getattr(caught.exception, 'uncertain', False))
            session.post.assert_called_once()

    def test_baidu_paid_redirect_is_uncertain_and_not_followed(self):
        _, _, source, target = self.gateway()
        session = Mock()
        session.post.return_value = SimpleNamespace(status_code=307)
        client = BaiduImageEnhance('fixture', 'fixture', session)
        with patch.object(client, 'get_token', return_value='fixture'):
            with self.assertRaises(BaiduError) as caught:
                client.enhance(source, target, quality='light')
        self.assertTrue(getattr(caught.exception, 'uncertain', False))
        self.assertIs(session.post.call_args.kwargs.get('allow_redirects'), False)
        session.post.assert_called_once()

    def test_text_completed_result_survives_housekeeping_failure(self):
        jid = 'textlatency1'
        m.users.reserve_job('sample_user', jid, 40, m.settings.quota(), False)
        m.users.confirm_job(jid, 'fixture')
        m.jobs.create(jid, openid='sample_user', input_mode='text', quality='light',
                      result_cos='results/fixture/text.jpg', result_file='text_latency.jpg')
        def generate(client, target, *args):
            Path(target).write_bytes(self.image())
        original = m.cleanup.schedule
        calls = []
        def schedule(*args):
            calls.append(args)
            if len(calls) >= 2:raise RuntimeError('housekeeping fixture failure')
            return original(*args)
        with patch.object(OpenAIImagesEnhance, 'generate', new=generate), patch.object(m, 'cos_put'), \
                patch.object(m, 'cos_head', return_value=True), patch.object(m.cleanup, 'schedule', side_effect=schedule):
            run_text_job(m, jid, {'model': 'fixture', 'endpoint': '/v1/images/generations'},
                         {'base_url': 'https://fixture.invalid', 'api_key': 'fixture'}, '画面', '1024x1024')
        self.assertEqual(m.jobs.get(jid)['status'], 'succeeded')
        self.assertEqual(m.users.get_balance('sample_user'), 60)
        self.assertTrue(Path(m.UPLOAD_DIR, 'text_latency.jpg').is_file())

    def test_text_completed_deleted_result_is_not_refunded_by_housekeeping(self):
        jid = 'textdeleted1'
        m.users.reserve_job('sample_user', jid, 40, m.settings.quota(), False)
        m.users.confirm_job(jid, 'fixture')
        m.jobs.create(jid, openid='sample_user', input_mode='text', quality='light',
                      result_cos='results/fixture/deleted.jpg', result_file='text_deleted.jpg')
        def generate(client, target, *args):Path(target).write_bytes(self.image())
        original = m.cleanup.schedule
        calls = []
        def schedule(*args):
            calls.append(args)
            if len(calls) >= 2:
                m.jobs.delete_for_openid(jid, 'sample_user')
                raise RuntimeError('housekeeping fixture failure after completed deletion')
            return original(*args)
        with patch.object(OpenAIImagesEnhance, 'generate', new=generate), patch.object(m, 'cos_put'), \
                patch.object(m, 'cos_head', return_value=True), patch.object(m.cleanup, 'schedule', side_effect=schedule):
            run_text_job(m, jid, {'model': 'fixture', 'endpoint': '/v1/images/generations'},
                         {'base_url': 'https://fixture.invalid', 'api_key': 'fixture'}, '画面', '1024x1024')
        self.assertEqual(m.jobs.get(jid)['status'], 'succeeded')
        self.assertTrue(m.jobs.get(jid)['deleted_at'])
        self.assertEqual(m.users.get_balance('sample_user'), 60)

    def test_simulated_dispatcher_delay_measurement(self):
        jid = self.new()
        clock = [time.time()]
        beginning = clock[0]
        submitted = []
        complete = {'status': 'completed', 'result': {'data': [{'url': 'https://' + cp.MEDIA_HOST + '/images/fixture.png'}]}}
        def submit(*args):
            submitted.append(clock[0])
            return 'imgtask_fixture'
        def poll(*args):
            return complete
        with self.immediate_dispatcher(), patch.object(cp.time, 'time', side_effect=lambda: clock[0]), \
                patch.object(AsyncImages, 'submit', side_effect=submit), patch.object(AsyncImages, 'poll', side_effect=poll):
            for _ in range(100):
                clock[0] += .25
                self.cloud.tick()
                if m.jobs.get(jid)['status'] != 'processing':
                    break
        job = m.jobs.get(jid)
        self.assertEqual(job['status'], 'succeeded', job.get('error'))
        self.assertEqual(len(submitted), 1)
        result = {'fixture': 'metadata operations take zero simulated time; provider first GET completed; poll interval 5 s',
                  'total_ms': round((clock[0] - beginning) * 1000),
                  'admit_to_paid_submit_ms': round((submitted[0] - beginning) * 1000),
                  'configured_poll_ms': 5000, 'paid_posts': len(submitted), 'live_provider_measured': False}
        (OUTPUT / 'pipeline_latency_measurement.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
        print('SIMULATED_PIPELINE_LATENCY ' + json.dumps(result, sort_keys=True))

    def test_simulated_backfill_delay_measurement(self):
        jid = self.importing()
        clock = [time.time()]
        beginning = clock[0]
        def metadata(*args):
            if clock[0] < beginning + 1.2:raise cp.cos.CosError('pending', status=404)
            return {'size': 1000}
        with self.immediate_dispatcher(), patch.object(cp.time, 'time', side_effect=lambda: clock[0]), \
                patch.object(cp.cos, 'object_metadata', side_effect=metadata), \
                patch.object(AsyncImages, 'submit', side_effect=AssertionError('PAID_RESUBMIT')):
            for _ in range(100):
                clock[0] += .25
                self.cloud.tick()
                if m.jobs.get(jid)['status'] != 'processing':break
        self.assertEqual(m.jobs.get(jid)['status'], 'succeeded', m.jobs.get(jid).get('error'))
        result = {'fixture': 'backfill object appears after 1200 simulated ms; metadata operations take zero time',
                  'total_ms': round((clock[0] - beginning) * 1000),
                  'mirror_triggers': cp.cos.trigger_mirror.call_count, 'paid_posts': 0,
                  'live_provider_measured': False}
        self.assertEqual(result['mirror_triggers'], 1)
        (OUTPUT / 'backfill_latency_measurement.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
        print('SIMULATED_BACKFILL_LATENCY ' + json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    suite = unittest.TestSuite(LatencyTests(name) for name in LatencyTests.__dict__ if name.startswith('test_'))
    names = [t._testMethodName for t in suite]
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    failed = {t._testMethodName for t, _ in result.failures + result.errors}
    (OUTPUT / 'review_pipeline_latency_results.json').write_text(json.dumps({
        'cases': [{'case': name, 'passed': name not in failed} for name in names]}, indent=2), encoding='utf-8')
    for conn in initial_connections:
        try:
            conn.close()
        except Exception:
            pass
    print('REVIEW_PIPELINE_LATENCY_SUMMARY total=%d passed=%d failed=%d' % (
        result.testsRun, result.testsRun - len(failed), len(failed)))
    raise SystemExit(0 if result.wasSuccessful() else 1)
