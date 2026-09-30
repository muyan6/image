"""Section-local text configuration reads; isolated data, no model/network calls."""
from pathlib import Path
import json
import os
import shutil
import statistics
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(os.environ.get('REVIEW_ROOT', Path(__file__).resolve().parents[2])).resolve()
OUTPUT = Path(os.environ.get('REVIEW_OUTPUT', ROOT / 'audit/sequential_20261001/text_config')).resolve()
OUTPUT.mkdir(parents=True, exist_ok=True)
fixture = tempfile.TemporaryDirectory(prefix='text_config_', dir=OUTPUT)
BASE = Path(fixture.name)
SOURCE = BASE / 'backend'
SOURCE.mkdir()
for path in (ROOT / 'backend').glob('*.py'):
    shutil.copy2(path, SOURCE / path.name)
sys.path.insert(0, str(SOURCE))
from settings_store import SettingsStore
from gateway_profiles import text_gateway
from text_generation import ready

observations = []


class TextConfigTests(unittest.TestCase):
    def setUp(self):
        self.settings = SettingsStore(str(BASE / self._testMethodName))
        self.settings.update({'text_generation': {'enabled': True, 'model': 'fixture-model',
            'base_url': 'https://fixture.invalid', 'api_key': 'fixture-only', 'metadata': {'nested': [1, 2]}}})

    def test_text_generation_section_is_detached(self):
        conf = self.settings.text_generation()
        conf['model'] = 'modified-return-value'
        conf['metadata']['nested'].append(3)
        actual = self.settings.snapshot()['text_generation']
        self.assertEqual(actual['model'], 'fixture-model')
        self.assertEqual(actual['metadata']['nested'], [1, 2])

    def test_gateway_projection_does_not_copy_unrelated_settings(self):
        expected = self.settings.snapshot()['text_generation']
        with patch.object(self.settings, 'snapshot', side_effect=AssertionError('UNRELATED_FULL_SNAPSHOT')):
            provider = text_gateway(self.settings)
        self.assertEqual(provider['base_url'], expected['base_url'])
        self.assertEqual(provider['api_key'], expected['api_key'])
        self.assertEqual(provider['model_light'], expected['model'])
        self.assertEqual(provider['request_timeout'], expected['timeout'])

    def test_ready_uses_one_consistent_text_configuration(self):
        runtime = SimpleNamespace(settings=self.settings, cloud=SimpleNamespace(enabled=lambda: False))
        with patch.object(self.settings, 'snapshot', side_effect=AssertionError('UNRELATED_FULL_SNAPSHOT')), \
                patch.object(self.settings, 'cos_ready', return_value=True), \
                patch.object(self.settings, 'text_generation', wraps=self.settings.text_generation) as getter:
            self.assertTrue(ready(runtime))
            self.assertEqual(getter.call_count, 1)

    def test_hot_update_and_explicit_blank_key_remain_effective(self):
        self.assertEqual(text_gateway(self.settings)['api_key'], 'fixture-only')
        self.settings.update({'text_generation': {'api_key': ''}})
        self.assertEqual(text_gateway(self.settings)['api_key'], '')
        self.settings.update({'text_generation': {'api_key': 'changed-fixture', 'model': 'changed-model'}})
        self.assertEqual(text_gateway(self.settings)['api_key'], 'changed-fixture')
        self.assertEqual(text_gateway(self.settings)['model_light'], 'changed-model')

    def test_configuration_microbenchmark(self):
        # Large unrelated community data must not affect a text-gateway lookup.
        with self.settings._lock:
            self.settings._data['community']['items'] = [
                {'title': 'fixture', 'story': 'x' * 900, 'covers': ['https://fixture.invalid/a'] * 3}
                for _ in range(200)]
        rounds = []
        for _ in range(5):
            beginning = time.perf_counter()
            for _ in range(100):
                self.assertEqual(text_gateway(self.settings)['model_light'], 'fixture-model')
            rounds.append((time.perf_counter() - beginning) * 1000 / 100)
        observation = {'fixture': '200 unrelated community items; 5 rounds x 100 lookups',
                       'median_lookup_ms': round(statistics.median(rounds), 5),
                       'live_generation_measured': False}
        observations.append(observation)
        print('TEXT_CONFIG_MICROBENCHMARK ' + json.dumps(observation), flush=True)


if __name__ == '__main__':
    names = [name for name in TextConfigTests.__dict__ if name.startswith('test_')]
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(TextConfigTests(name) for name in names))
    failed = {test._testMethodName for test, _ in result.failures + result.errors}
    (OUTPUT / 'sequential_text_config_results.json').write_text(json.dumps({
        'cases': [{'case': name, 'passed': name not in failed} for name in names],
        'observations': observations}, ensure_ascii=False, indent=2), encoding='utf-8')
    fixture.cleanup()
    print('SEQUENTIAL_TEXT_CONFIG_SUMMARY total=%d passed=%d failed=%d' % (result.testsRun, result.testsRun - len(failed), len(failed)))
    raise SystemExit(0 if result.wasSuccessful() else 1)
