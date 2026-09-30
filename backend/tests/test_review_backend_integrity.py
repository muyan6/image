"""Offline backend integrity regressions; copied code, temporary state, no network."""
import copy
import json
import os
from pathlib import Path
import shutil
import statistics
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(os.environ.get('REVIEW_ROOT', Path(__file__).resolve().parents[2])).resolve()
OUTPUT = Path(os.environ.get('REVIEW_OUTPUT', ROOT / 'audit/review-backend-integrity')).resolve()
OUTPUT.mkdir(parents=True, exist_ok=True)
fixture = tempfile.TemporaryDirectory(prefix='backend_integrity_', dir=OUTPUT)
SOURCE = Path(fixture.name) / 'backend'
SOURCE.mkdir()
for path in (ROOT / 'backend').glob('*.py'):
    shutil.copy2(path, SOURCE / path.name)
os.environ.update(ADMIN_PASSWORD='backend-integrity-fixture', DATA_DIR=str(SOURCE / 'data'),
                  UPLOAD_DIR=str(SOURCE / 'uploads'), LOG_LEVEL='CRITICAL')
sys.path.insert(0, str(SOURCE))
import requests
network_guard = patch.object(requests.sessions.Session, 'request', side_effect=AssertionError('NETWORK_DISABLED'))
network_guard.start()
import settings_store
import templates_store
observations = []


class BackendIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='state_', dir=OUTPUT)
        self.data = Path(self.temp.name)
        self.env = patch.dict(os.environ, DATA_DIR=str(self.data))
        self.env.start()
        self.placeholder = patch.object(templates_store, 'generate_placeholder_cover', return_value=b'fixture-cover')
        self.make_placeholder = self.placeholder.start()

    def tearDown(self):
        self.placeholder.stop()
        self.env.stop()
        self.temp.cleanup()

    def template(self, store, tid='fixture_cover'):
        group = store.list_groups()[0]['id']
        return store.create_template({'id': tid, 'group_id': group,
                                      'name': '封面验证', 'prompt': '保持画面'})

    def test_rejected_announcement_update_preserves_memory_and_disk(self):
        store = settings_store.AnnouncementStore(str(self.data))
        item = store.create('原公告', '原正文')
        before = store.list_all()
        disk = Path(store._path).read_bytes()
        with self.assertRaises(ValueError):
            store.update(item['id'], {'title': '失败请求的标题', 'level': 'invalid'})
        self.assertEqual(store.list_all(), before)
        self.assertEqual(Path(store._path).read_bytes(), disk)
        # A later successful write must not flush a rejected change to disk.
        store.create('下一条', '新正文')
        self.assertEqual(settings_store.AnnouncementStore(str(self.data)).list_all()[0], item)

    def test_announcement_io_failure_restores_memory(self):
        store = settings_store.AnnouncementStore(str(self.data))
        item = store.create('原公告', '原正文')
        before = store.list_all()
        with patch.object(store, '_save_locked', side_effect=OSError('fixture write failed')):
            with self.assertRaises(OSError):
                store.update(item['id'], {'title': '未保存的标题'})
        self.assertEqual(store.list_all(), before)

    def test_valid_announcement_update_persists(self):
        store = settings_store.AnnouncementStore(str(self.data))
        item = store.create('原公告', '原正文')
        changed = store.update(item['id'], {'title': '新公告', 'body': '新正文', 'enabled': False})
        self.assertEqual(changed['title'], '新公告')
        self.assertFalse(changed['enabled'])
        self.assertEqual(settings_store.AnnouncementStore(str(self.data)).list_all(), [changed])

    def test_rejected_group_update_preserves_memory_and_disk(self):
        store = templates_store.TemplateStore(str(self.data))
        group = store.list_groups()[0]
        before = store.list_groups()
        disk = Path(store._path).read_bytes()
        with self.assertRaises(ValueError):
            store.update_group(group['id'], {'name': '失败请求的新名称', 'sort': 'invalid'})
        self.assertEqual(store.list_groups(), before)
        self.assertEqual(Path(store._path).read_bytes(), disk)
        store.create_group('下一组')
        self.assertIn(group, templates_store.TemplateStore(str(self.data)).list_groups())

    def test_group_io_failure_restores_memory(self):
        store = templates_store.TemplateStore(str(self.data))
        group = store.list_groups()[0]
        before = store.list_groups()
        with patch.object(store, '_save_locked', side_effect=OSError('fixture write failed')):
            with self.assertRaises(OSError):
                store.update_group(group['id'], {'name': '未保存的名称'})
        self.assertEqual(store.list_groups(), before)

    def test_valid_group_update_persists(self):
        store = templates_store.TemplateStore(str(self.data))
        group = store.list_groups()[0]
        changed = store.update_group(group['id'], {'name': '新分组', 'sort': 42, 'enabled': False})
        self.assertEqual(changed['name'], '新分组')
        self.assertFalse(changed['enabled'])
        self.assertIn(changed, templates_store.TemplateStore(str(self.data)).list_groups())

    def test_existing_uploaded_cover_survives_restart(self):
        store = templates_store.TemplateStore(str(self.data))
        self.template(store)
        name = 'fixture_cover_s0_v1.jpg'
        cover_path = Path(templates_store.covers_dir()) / name
        cover_path.write_bytes(b'uploaded-example')
        original = store.set_cover_slot('fixture_cover', 0, 'local:' + name)
        # No v1 placeholder exists for a freshly created/uploaded custom template.
        restored = templates_store.TemplateStore(str(self.data)).get_template('fixture_cover')
        self.assertEqual(restored['cover'], original['cover'])
        self.assertEqual(restored['covers'], original['covers'])
        self.assertEqual(cover_path.read_bytes(), b'uploaded-example')
        self.assertFalse((cover_path.parent / 'fixture_cover_v1.jpg').exists())

    def test_missing_primary_cover_repairs_public_reference_and_keeps_other_examples(self):
        store = templates_store.TemplateStore(str(self.data))
        self.template(store, 'fixture_repair')
        second = 'fixture_repair_s1_v2.jpg'
        (Path(templates_store.covers_dir()) / second).write_bytes(b'second-example')
        store.set_cover_slot('fixture_repair', 0, 'local:missing-primary.jpg')
        store.set_cover_slot('fixture_repair', 1, 'local:' + second)
        restored = templates_store.TemplateStore(str(self.data)).get_template('fixture_repair')
        self.assertEqual(restored['cover'], 'local:fixture_repair_v1.jpg')
        self.assertEqual(restored['covers'], ['local:fixture_repair_v1.jpg', 'local:' + second])
        self.assertIn('fixture_repair_v1.jpg', templates_store.resolve_cover(restored, None))

    def test_covers_directory_follows_data_dir_without_stale_cache(self):
        first = Path(templates_store.covers_dir()).resolve()
        self.assertEqual(first, (self.data / 'covers').resolve())
        other = self.data / 'other'
        with patch.dict(os.environ, DATA_DIR=str(other)):
            self.assertEqual(Path(templates_store.covers_dir()).resolve(), (other / 'covers').resolve())
        self.assertEqual(Path(templates_store.covers_dir()).resolve(), first)

    def test_covers_directory_default_is_unchanged(self):
        with patch.dict(os.environ):
            os.environ.pop('DATA_DIR', None)
            default = Path(templates_store._covers_dir()).resolve()
        self.assertEqual(default, (Path(templates_store.__file__).parent / 'data/covers').resolve())

    def settings(self, posts=False):
        store = settings_store.SettingsStore(str(self.data))
        if posts:
            store.update({'community': {'enabled': True, 'posts_version': 1, 'items': [
                {'id': 'p_%03d' % index, 'title': '示例 %d' % index, 'story': '作品说明' * 1000,
                 'author_name': '示例作者', 'author_avatar': 'https://fixture.invalid/avatar.jpg',
                 'template_id': 'fixture', 'template_name': '示例风格', 'category': 'portrait',
                 'category_name': '人像', 'quality': 'fine', 'result_url': 'https://fixture.invalid/result.jpg',
                 'orig_url': 'https://fixture.invalid/original.jpg', 'likes': index, 'status': 'published',
                 'pinned': False, 'sort': index, 'created_at': 1000 + index, 'updated_at': 2000 + index}
                for index in range(200)]}})
        return store

    def getters(self, store):
        return [store.chain, lambda: store.provider('worldcodes'),
                lambda: store.provider_enabled('worldcodes'), lambda: store.gateway_for('light'),
                lambda: store.prompt_for('light'), store.prices, store.rewards, store.free_mode,
                store.wechat, store.tencent, store.moderation, store.quota, store.cos_ready,
                store.moderation_ready, store.maintenance, store.normalize_long_side,
                store.styles, store.quality_to_style, store.community, store.ads]

    def test_settings_getters_preserve_detached_values(self):
        store = self.settings(posts=True)
        before = store.snapshot()
        for getter in self.getters(store):
            value = getter()
            if isinstance(value, dict):
                value.clear()
                value['fixture-mutated'] = True
            elif isinstance(value, list):
                value.clear()
        nested = store.provider('worldcodes')
        nested['tiers']['light']['api_key'] = 'fixture-not-a-real-key'
        posts = store.community()
        posts['items'][0]['title'] = '客户端修改不得污染配置'
        self.assertEqual(store.snapshot(), before)
        self.assertEqual(store.provider('missing'), {})
        self.assertEqual(store.prompt_for('unknown'), before['prompts']['fine'])

    def test_settings_hot_getters_do_not_clone_unrelated_sections(self):
        store = self.settings(posts=True)
        expected = [getter() for getter in self.getters(store)]
        with patch.object(store, 'snapshot', side_effect=AssertionError('FULL_SNAPSHOT_ON_HOT_PATH')):
            self.assertEqual([getter() for getter in self.getters(store)], expected)

    def test_cloud_pipeline_getter_is_detached_and_current(self):
        store = self.settings()
        self.assertTrue(callable(getattr(store, 'cloud_pipeline', None)))
        before = store.snapshot()['cloud_pipeline']
        returned = store.cloud_pipeline()
        returned['generation_concurrency'] = 1
        self.assertEqual(store.cloud_pipeline(), before)
        store.update({'cloud_pipeline': {'generation_concurrency': 8}})
        self.assertEqual(store.cloud_pipeline()['generation_concurrency'], 8)

    def test_settings_reads_stay_consistent_during_threaded_updates(self):
        store = self.settings()
        barrier = threading.Barrier(4)
        done = threading.Event()
        errors = []
        reads = []
        def writer():
            try:
                barrier.wait()
                for value in range(1, 41):
                    store.update({'prices': {'light': value, 'fine': value}})
            except BaseException as exc:
                errors.append(exc)
            finally:
                done.set()
        def reader():
            try:
                barrier.wait()
                count = 0
                while not done.is_set() or count < 100:
                    prices = store.prices()
                    if prices['light'] != prices['fine']:
                        raise AssertionError('TORN_PRICE_SNAPSHOT')
                    prices['light'] = 9999
                    count += 1
                reads.append(count)
            except BaseException as exc:
                errors.append(exc)
        threads = [threading.Thread(target=writer)] + [threading.Thread(target=reader) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(store.prices(), {'light': 40, 'fine': 40})
        self.assertEqual(len(reads), 3)

    def test_settings_hot_path_cpu_microbenchmark(self):
        store = self.settings(posts=True)
        pipeline = getattr(store, 'cloud_pipeline', lambda: store.snapshot()['cloud_pipeline'])
        hot_reads = [store.maintenance, store.wechat, store.tencent, store.moderation,
                     store.quota, store.prices, lambda: store.provider('worldcodes'),
                     lambda: store.prompt_for('light'), pipeline]
        def batch():
            return [getter() for getter in hot_reads]
        expected = batch()
        rounds = []
        for _ in range(5):
            started = time.perf_counter()
            for _ in range(100):
                self.assertEqual(batch(), expected)
            rounds.append((time.perf_counter() - started) * 1000 / 100)
        observation = {'benchmark': 'nine_settings_reads', 'community_posts': 200,
                       'iterations_per_round': 100, 'rounds': 5,
                       'median_batch_ms': round(statistics.median(rounds), 4),
                       'measurement': 'local CPU microbenchmark; not upstream generation latency'}
        observations.append(observation)
        print('SETTINGS_CPU_MICROBENCHMARK ' + json.dumps(observation, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(BackendIntegrityTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    failed = {test._testMethodName for test, _ in result.failures + result.errors}
    cases = [{'case': name, 'passed': name not in failed}
             for name in sorted(BackendIntegrityTests.__dict__) if name.startswith('test_')]
    (OUTPUT / 'review_backend_integrity_results.json').write_text(
        json.dumps({'cases': cases, 'observations': observations}, ensure_ascii=False, indent=2), encoding='utf-8')
    network_guard.stop()
    fixture.cleanup()
    print('REVIEW_BACKEND_INTEGRITY_SUMMARY total=%d passed=%d failed=%d' %
          (result.testsRun, result.testsRun - len(failed), len(failed)), flush=True)
    raise SystemExit(0 if result.wasSuccessful() else 1)
