"""Loopback HTTP/1.1 keepalive measurements and metadata wire invariants.

Only 127.0.0.1 sockets are used. No real gateway, COS or model is contacted.
The keepalive assertions intentionally fail against the pre-optimization source.
"""
import base64
import hashlib
import importlib
import json
import os
import socket
import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(os.environ.get('REVIEW_ROOT', Path(__file__).resolve().parents[2])).resolve()
OUTPUT = Path(os.environ.get('REVIEW_OUTPUT', ROOT / 'audit/sequential_20261001/transport')).resolve()
OUTPUT.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT / 'backend'))
import requests
import cos_store
import gateway_async
from gateway_async import AsyncImages, GatewayAsyncError

MEASUREMENTS = {}


class FixtureSettings:
    def tencent(self):
        return {'secret_id': 'fixture', 'secret_key': 'fixture', 'cos_bucket': 'fixture-123456',
                'cos_region': 'ap-guangzhou', 'cos_custom_domain': ''}


class KeepaliveServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self):
        self.lock = threading.Lock()
        self.accepted = []
        self.requests_seen = []
        self.drop_submit = False
        super().__init__(('127.0.0.1', 0), Handler)

    def get_request(self):
        connection, address = super().get_request()
        connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        with self.lock:self.accepted.append((connection, address))
        return connection, address

    def close_connections(self):
        for connection, _ in self.accepted:
            try:connection.shutdown(socket.SHUT_RDWR)
            except OSError:pass
            try:connection.close()
            except OSError:pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def handle_one_request(self):
        try:super().handle_one_request()
        except (ConnectionError, OSError):self.close_connection = True

    def record(self):
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length) if length else b''
        with self.server.lock:
            self.server.requests_seen.append({'method': self.command, 'path': self.path,
                'client_port': self.client_address[1], 'headers': dict(self.headers),
                'body': body.decode('utf-8')})

    def reply(self, status, body=b'{"status":"running"}', extra=None):
        self.send_response(status)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Content-Type', 'application/json')
        for key, value in (extra or {}).items():self.send_header(key, value)
        self.end_headers()
        if self.command != 'HEAD':
            try:self.wfile.write(body);self.wfile.flush()
            except OSError:self.close_connection = True

    def route(self):
        self.record()
        if self.command == 'POST' and self.server.drop_submit:
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
        elif self.path.startswith('/redirect'):
            self.reply(307, b'{"task_id":"imgtask_fixture"}', {'Location': '/target'})
        elif self.path == '/set-cookie':
            self.reply(200, extra={'Set-Cookie': 'transport_fixture=must_not_persist; Path=/'})
        elif self.command == 'POST':
            self.reply(202, b'{"task_id":"imgtask_fixture"}')
        else:self.reply(200)

    do_GET = route
    do_HEAD = route
    do_POST = route
    do_PUT = route


def response(status=200, body=b'{}', headers=None):
    result = Mock(status_code=status, headers=headers or {}, content=body)
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    result.iter_content = Mock(return_value=[body])
    return result


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.server = KeepaliveServer()
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=.01), daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:%d' % self.server.server_port
        self.host = '127.0.0.1:%d' % self.server.server_port

    def tearDown(self):
        transport = sys.modules.get('http_transport')
        if transport is not None:transport.close_current_thread()
        self.server.shutdown()
        self.server.close_connections()
        self.server.server_close()
        self.thread.join(timeout=2)

    def gateway(self, key='fixture'):
        client = AsyncImages({'base_url': 'https://fixture.invalid', 'api_key': key, 'request_timeout': 1})
        # Restrict this test-only transport substitution to a bound loopback server.
        client.base = self.base
        return client

    def transport(self):
        try:return importlib.import_module('http_transport')
        except ImportError:self.fail('thread-local http_transport helper is not present in the baseline')

    def cos_url(self, settings, method, key='', params=None, headers=None):
        return self.base + '/' + key, self.host

    def measure(self, name, requests_count, started):
        value = {'requests': requests_count, 'accepted_tcp_connections': len(self.server.accepted),
                 'wall_ms': round((time.monotonic() - started) * 1000, 3),
                 'transport': 'real loopback HTTP/1.1; no TLS; no supplier/model',
                 'request_count_observed': len(self.server.requests_seen)}
        MEASUREMENTS[name] = value
        return value

    def test_gateway_reuses_connection_across_new_clients_on_same_worker(self):
        started = time.monotonic()
        for _ in range(8):self.gateway().poll('imgtask_fixture')
        measured = self.measure('gateway_same_worker', 8, started)
        self.assertEqual(measured['request_count_observed'], 8)
        self.assertEqual(measured['accepted_tcp_connections'], 1)

    def test_cos_reuses_connection_across_control_operations(self):
        started = time.monotonic()
        with patch.object(cos_store, 'control_url', side_effect=self.cos_url):
            for index in range(8):
                result = cos_store.control_request(FixtureSettings(), 'HEAD' if index % 2 else 'GET', 'metadata')
                self.assertEqual(result.status_code, 200)
                result.close()
        measured = self.measure('cos_same_worker', 8, started)
        self.assertEqual(measured['request_count_observed'], 8)
        self.assertEqual(measured['accepted_tcp_connections'], 1)

    def test_four_workers_keep_independent_connections(self):
        barrier = threading.Barrier(4)
        started = time.monotonic()
        def worker(number):
            client = self.gateway('worker-%d' % number)
            barrier.wait(timeout=3)
            for _ in range(4):client.poll('imgtask_fixture')
        with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(worker, range(4)))
        measured = self.measure('gateway_four_workers', 16, started)
        self.assertEqual(measured['request_count_observed'], 16)
        ports = {}
        for record in self.server.requests_seen:
            ports.setdefault(record['headers']['Authorization'], set()).add(record['client_port'])
        self.assertEqual(len(ports), 4)
        self.assertTrue(all(len(group) == 1 for group in ports.values()))
        self.assertEqual(len(set.union(*ports.values())), 4)
        self.assertEqual(measured['accepted_tcp_connections'], 4)

    def test_paid_post_accepted_then_disconnected_is_not_replayed(self):
        self.server.drop_submit = True
        with self.assertRaises(GatewayAsyncError) as caught:self.gateway().submit('fixture', 'fixture')
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(len(self.server.requests_seen), 1)
        self.assertEqual(self.server.requests_seen[0]['method'], 'POST')
        self.assertEqual(json.loads(self.server.requests_seen[0]['body'])['n'], 1)

    def test_paid_redirect_is_not_followed_or_replayed(self):
        with self.assertRaises(GatewayAsyncError) as caught:
            self.gateway().submit('fixture', 'fixture', endpoint='/redirect')
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(len(self.server.requests_seen), 1)
        self.assertEqual(self.server.requests_seen[0]['path'], '/redirect/async')

    def test_cos_redirect_is_not_followed(self):
        with patch.object(cos_store, 'control_url', side_effect=self.cos_url):
            with cos_store.control_request(FixtureSettings(), 'GET', 'redirect') as result:
                self.assertEqual(result.status_code, 307)
        self.assertEqual(len(self.server.requests_seen), 1)

    def test_connection_reuse_does_not_persist_server_cookies(self):
        client = self.gateway()
        client.request('GET', '/set-cookie')
        client.poll('imgtask_fixture')
        self.assertNotIn('Cookie', self.server.requests_seen[-1]['headers'])

    def test_credentials_remain_per_request_and_do_not_leak_to_cos(self):
        self.gateway('fixture-a').poll('imgtask_fixture')
        self.gateway('fixture-b').poll('imgtask_fixture')
        with patch.object(cos_store, 'control_url', side_effect=self.cos_url):
            with cos_store.control_request(FixtureSettings(), 'HEAD', 'metadata'):pass
        seen = self.server.requests_seen
        self.assertEqual(seen[0]['headers']['Authorization'], 'Bearer fixture-a')
        self.assertEqual(seen[1]['headers']['Authorization'], 'Bearer fixture-b')
        self.assertNotIn('Authorization', seen[2]['headers'])

    def test_gateway_wire_contract_closes_bounded_stream(self):
        result = response(202, b'{"task_id":"imgtask_fixture"}')
        with patch.object(requests.sessions.Session, 'request', return_value=result) as request:
            self.assertEqual(self.gateway().submit('fixture', 'fixture'), 'imgtask_fixture')
        self.assertEqual(request.call_count, 1)
        kw = request.call_args.kwargs
        self.assertFalse(kw['allow_redirects'])
        self.assertTrue(kw['stream'])
        self.assertEqual(kw['timeout'], (5, 1))
        self.assertEqual(kw['headers']['Authorization'], 'Bearer fixture')
        self.assertEqual(kw['json'], {'model': 'fixture', 'prompt': 'fixture', 'n': 1})
        result.__exit__.assert_called_once()

    def test_gateway_oversize_metadata_closes_response_and_marks_paid_uncertain(self):
        result = response(202, b'x' * (256 * 1024 + 1))
        with patch.object(requests.sessions.Session, 'request', return_value=result):
            with self.assertRaises(GatewayAsyncError) as caught:self.gateway().submit('fixture', 'fixture')
        self.assertTrue(caught.exception.uncertain)
        result.__exit__.assert_called_once()

    def test_cos_mirror_range_closes_response_without_reading_image_body(self):
        result = response(206, b'IMAGE_BODY_MUST_NOT_BE_READ', {'x-cos-request-id': 'fixture-id'})
        with patch.object(requests.sessions.Session, 'request', return_value=result) as request:
            self.assertTrue(cos_store.trigger_mirror(FixtureSettings(), 'images/fixture.jpg')['accepted'])
        self.assertEqual(request.call_args.kwargs['headers']['Range'], 'bytes=0-0')
        self.assertTrue(request.call_args.kwargs['stream'])
        self.assertFalse(request.call_args.kwargs['allow_redirects'])
        result.iter_content.assert_not_called()
        result.__exit__.assert_called_once()

    def test_cos_control_put_preserves_direct_host_md5_and_timeouts(self):
        result = response()
        with patch.object(requests.sessions.Session, 'request', return_value=result) as request:
            cos_store.control_request(FixtureSettings(), 'PUT', params={'origin': ''}, data=b'<fixture/>')
        kw = request.call_args.kwargs
        self.assertEqual(kw['headers']['Content-MD5'], base64.b64encode(hashlib.md5(b'<fixture/>').digest()).decode())
        self.assertEqual(kw['headers']['Host'], 'fixture-123456.cos.ap-guangzhou.myqcloud.com')
        self.assertEqual(kw['timeout'], (5, 30))
        self.assertFalse(kw['allow_redirects'])
        self.assertFalse(kw['stream'])

    def test_reference_persistent_session_measures_connection_reduction(self):
        started = time.monotonic()
        with requests.Session() as persistent:
            # Reference only: temporary fixture injection, no source edits.
            target = getattr(gateway_async, 'http_transport', requests)
            with patch.object(target, 'request', new=persistent.request):
                for _ in range(8):self.gateway().poll('imgtask_fixture')
        measured = self.measure('reference_persistent_session', 8, started)
        self.assertEqual(measured['request_count_observed'], 8)
        self.assertEqual(measured['accepted_tcp_connections'], 1)

    def test_same_thread_session_reuse_and_defaults_remain_clean(self):
        transport = self.transport()
        transport.close_all()
        session = transport.get_session()
        self.assertIs(session, transport.get_session())
        self.gateway('fixture-clean').poll('imgtask_fixture')
        with patch.object(cos_store, 'control_url', side_effect=self.cos_url):
            with cos_store.control_request(FixtureSettings(), 'HEAD', 'metadata'):pass
        self.assertIsNone(session.auth)
        self.assertNotIn('Authorization', session.headers)
        self.assertNotIn('Host', session.headers)
        self.assertFalse(session.cookies)
        for adapter in session.adapters.values():self.assertEqual(adapter.max_retries.total, 0)

    def test_current_thread_close_is_idempotent_and_recreates_session(self):
        transport = self.transport()
        transport.close_all()
        original = transport.get_session()
        with patch.object(original, 'close', wraps=original.close) as close:
            transport.close_current_thread()
            transport.close_current_thread()
        close.assert_called_once()
        renewed = transport.get_session()
        self.assertIsNot(original, renewed)
        transport.close_current_thread()

    def test_close_all_closes_live_worker_sessions_and_allows_recreation(self):
        transport = self.transport()
        transport.close_all()
        main_session = transport.get_session()
        ready, proceed = threading.Event(), threading.Event()
        worker_sessions = []
        def worker():
            worker_sessions.append(transport.get_session())
            ready.set()
            self.assertTrue(proceed.wait(timeout=3))
            worker_sessions.append(transport.get_session())
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(worker)
            self.assertTrue(ready.wait(timeout=3))
            with patch.object(main_session, 'close', wraps=main_session.close) as main_close, \
                    patch.object(worker_sessions[0], 'close', wraps=worker_sessions[0].close) as worker_close:
                try:
                    transport.close_all()
                    transport.close_all()
                    self.assertIsNot(main_session, transport.get_session())
                    main_close.assert_called_once()
                    worker_close.assert_called_once()
                finally:proceed.set()
            future.result(timeout=3)
        self.assertIsNot(worker_sessions[0], worker_sessions[1])
        self.assertIsNot(worker_sessions[1], transport.get_session())
        transport.close_current_thread()

    def test_live_threads_do_not_share_session_objects(self):
        transport = self.transport()
        transport.close_all()
        barrier = threading.Barrier(4)
        def worker(_):
            session = transport.get_session()
            barrier.wait(timeout=3)
            self.assertIs(session, transport.get_session())
            return session
        with ThreadPoolExecutor(max_workers=4) as pool:sessions = list(pool.map(worker, range(4)))
        self.assertEqual(len({id(session) for session in sessions}), 4)

    def test_exited_threads_close_sessions_without_global_strong_retention(self):
        transport = self.transport()
        transport.close_all()
        created, closed = [], []
        original_close = requests.Session.close
        def tracked_close(session):
            closed.append(id(session))
            return original_close(session)
        def worker():created.append(id(transport.get_session()))
        with patch.object(requests.Session, 'close', new=tracked_close):
            for _ in range(12):
                thread = threading.Thread(target=worker)
                thread.start()
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive())
        # Every thread's own Session closes as its TLS owner disappears.
        # A strong process-wide session registry would prevent this transition.
        self.assertEqual(len(closed), 12)
        self.assertEqual(sorted(created), sorted(closed))


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(TransportTests)
    names = [test._testMethodName for test in suite]
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    failed = {test._testMethodName for test, _ in result.failures + result.errors}
    (OUTPUT / 'sequential_transport_results.json').write_text(json.dumps({
        'cases': [{'case': name, 'passed': name not in failed} for name in names]}, indent=2), encoding='utf-8')
    (OUTPUT / 'sequential_transport_measurements.json').write_text(json.dumps(MEASUREMENTS, indent=2), encoding='utf-8')
    print('SEQUENTIAL_TRANSPORT_MEASUREMENTS ' + json.dumps(MEASUREMENTS, sort_keys=True))
    print('SEQUENTIAL_TRANSPORT_SUMMARY total=%d passed=%d failed=%d' % (
        result.testsRun, result.testsRun - len(failed), len(failed)))
    raise SystemExit(0 if result.wasSuccessful() else 1)
