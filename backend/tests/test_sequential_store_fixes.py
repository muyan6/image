"""Sequential store bug reproduction: isolated JSON stores and real write-path faults.

This module does not import main, account stores, payment code or live settings.
REVIEW_ROOT supports baseline/modified/rollback source copies without changing tests.
"""
import copy
import errno
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(os.environ.get('REVIEW_ROOT', Path(__file__).resolve().parents[2])).resolve()
OUTPUT = Path(os.environ.get('REVIEW_OUTPUT', ROOT / 'audit/sequential_20261001/store_baseline')).resolve()
OUTPUT.mkdir(parents=True, exist_ok=True)
fixture = tempfile.TemporaryDirectory(prefix='store_source_', dir=OUTPUT)
SOURCE = Path(fixture.name) / 'backend'
SOURCE.mkdir()
for name in ('settings_store.py', 'templates_store.py', 'gateway_profiles.py', 'gateway_registry.py', 'credit_packages.py',
             'template_share_rewards.py', 'account_links.py'):
    dependency = ROOT / 'backend' / name
    # Reward policy validation is now a real settings dependency. Historical
    # baseline/rollback checkouts do not contain it and do not import it either.
    if dependency.is_file(): shutil.copy2(dependency, SOURCE / name)
sys.path.insert(0, str(SOURCE))
import settings_store
import templates_store

observations = []


def snapshot(store):
    if isinstance(store, settings_store.AnnouncementStore):
        return copy.deepcopy(store._items)
    return {'groups': copy.deepcopy(store._groups), 'templates': copy.deepcopy(store._templates)}


def disk_snapshot(store):
    doc = json.loads(Path(store._path).read_text(encoding='utf-8'))
    if isinstance(doc, list):
        return doc
    return {key: doc[key] for key in ('groups', 'templates')}


class SequentialStoreTests(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.TemporaryDirectory(prefix='state_', dir=OUTPUT)
        self.data = Path(self.state.name)
        self.environment = patch.dict(os.environ, DATA_DIR=str(self.data))
        self.environment.start()
        self.placeholders = patch.object(templates_store, 'generate_placeholder_cover', return_value=b'fixture-cover')
        self.placeholders.start()

    def tearDown(self):
        self.placeholders.stop()
        self.environment.stop()
        self.state.cleanup()

    def announcement(self):
        store = settings_store.AnnouncementStore(str(self.data))
        item = store.create('原公告', '原正文')
        return store, item['id']

    def template(self):
        with patch.object(templates_store.TemplateStore, 'ensure_placeholder_covers'):
            store = templates_store.TemplateStore(str(self.data))
        group = store.create_group('独立分组')
        item = store.create_template({'id': 'fixture_original', 'group_id': 'restore',
                                      'name': '原模板', 'prompt': '保留画面',
                                      'cover': 'https://fixture.invalid/original.jpg'})
        store.set_cover_slot(item['id'], 1, 'https://fixture.invalid/second.jpg')
        return store, item['id'], group['id']

    def case(self, name):
        if name.startswith('announcement_'):
            store, item_id = self.announcement()
            operations = {
                'announcement_create': lambda: store.create('新公告', '新正文'),
                'announcement_update': lambda: store.update(item_id, {'title': '新标题', 'enabled': False}),
                'announcement_delete': lambda: store.delete(item_id),
            }
            return store, operations[name]
        store, item_id, group_id = self.template()
        operations = {
            'group_create': lambda: store.create_group('新分组'),
            'group_update': lambda: store.update_group(group_id, {'name': '新名称', 'sort': 77}),
            'group_delete': lambda: store.delete_group(group_id),
            'template_create': lambda: store.create_template({'id': 'fixture_created', 'group_id': 'restore',
                                                              'name': '新模板', 'prompt': '保持主体',
                                                              'cover': 'https://fixture.invalid/new.jpg'}),
            'template_update': lambda: store.update_template(item_id, {'name': '新模板名称', 'price': 88}),
            'template_delete': lambda: store.delete_template(item_id),
            'template_inc_usage': lambda: store.inc_usage(item_id),
            'template_reprice': lambda: store.reprice_by_engine({'light': 80, 'fine': 90}),
            'cover_set': lambda: store.set_cover_slot(item_id, 0, 'https://fixture.invalid/replaced.jpg'),
            'cover_legacy_set': lambda: store.set_cover(item_id, 'https://fixture.invalid/legacy.jpg'),
            'cover_delete': lambda: store.delete_cover_slot(item_id, 0),
            'cover_placeholder': store.ensure_placeholder_covers,
        }
        return store, operations[name]

    def reopen(self, store):
        if isinstance(store, settings_store.AnnouncementStore):
            return settings_store.AnnouncementStore(str(self.data))
        with patch.object(templates_store.TemplateStore, 'ensure_placeholder_covers'):
            return templates_store.TemplateStore(str(self.data))

    def fail_write(self, store, mode):
        target = os.path.abspath(store._path)
        tmp = target + '.tmp'
        hits = []
        if mode == 'open_write':
            import builtins
            original = builtins.open
            def failing_open(path, *args, **kwargs):
                access = args[0] if args else kwargs.get('mode', 'r')
                if isinstance(path, (str, bytes, os.PathLike)) and os.path.abspath(path) == tmp and 'w' in access:
                    hits.append('open_write')
                    raise OSError(errno.ENOSPC, 'fixture disk full')
                return original(path, *args, **kwargs)
            return patch('builtins.open', side_effect=failing_open), hits
        original = os.replace
        def failing_replace(source, destination, *args, **kwargs):
            if os.path.abspath(destination) == target:
                hits.append('replace')
                raise PermissionError(errno.EACCES, 'fixture replace denied')
            return original(source, destination, *args, **kwargs)
        return patch.object(os, 'replace', side_effect=failing_replace), hits

    def assert_atomic_failure(self, name, mode):
        store, operation = self.case(name)
        before = snapshot(store)
        original_file = Path(store._path).read_bytes()
        fault, hits = self.fail_write(store, mode)
        with fault:
            with self.assertRaises(OSError):
                operation()
        memory_restored = snapshot(store) == before
        file_unchanged = Path(store._path).read_bytes() == original_file
        reopened_before = snapshot(self.reopen(store)) == before
        # A later successful user write should not accidentally commit the rejected operation.
        if isinstance(store, settings_store.AnnouncementStore):
            later = store.create('后续正常公告', '正常正文')
            after_other_write = [item for item in disk_snapshot(store) if item['id'] != later['id']]
        else:
            later = store.create_group('后续正常分组')
            after_other_write = disk_snapshot(store)
            after_other_write['groups'] = [group for group in after_other_write['groups'] if group['id'] != later['id']]
        no_rejected_write_leaked = after_other_write == before
        observations.append({'case': name, 'fault': mode, 'fault_hits': len(hits),
                             'memory_restored': memory_restored, 'original_file_unchanged': file_unchanged,
                             'reopen_matches_before': reopened_before,
                             'later_success_did_not_persist_rejected_write': no_rejected_write_leaked})
        self.assertEqual(len(hits), 1, 'fault must reach the real store write path')
        self.assertTrue(file_unchanged, 'the original JSON must remain byte-identical')
        self.assertTrue(reopened_before, 'a reopened store must retain its original content')
        self.assertTrue(memory_restored, 'failed disk write must leave the live store unchanged')
        self.assertTrue(no_rejected_write_leaked, 'a later write must not persist the rejected operation')

    def assert_success_persists(self, name):
        store, operation = self.case(name)
        before = snapshot(store)
        operation()
        after = snapshot(store)
        self.assertNotEqual(after, before, 'the selected write must actually change state')
        self.assertEqual(after, disk_snapshot(store))
        self.assertEqual(after, snapshot(self.reopen(store)))

    def assert_load_io_preserves_file(self, kind, mode, migration=False):
        store = self.announcement()[0] if kind == 'announcement' else self.template()[0]
        path = Path(store._path)
        if migration:
            doc = json.loads(path.read_text(encoding='utf-8'))
            doc['version'] = 2
            doc['templates'][-1]['price'] = 17
            path.write_text(json.dumps(doc, ensure_ascii=False), encoding='utf-8')
        before = path.read_bytes()
        hits = []
        if mode == 'read':
            import builtins
            original = builtins.open
            def failing_read(filename, *args, **kwargs):
                access = args[0] if args else kwargs.get('mode', 'r')
                if isinstance(filename, (str, bytes, os.PathLike)) and os.path.abspath(filename) == str(path) and 'r' in access:
                    hits.append('read')
                    raise PermissionError(errno.EACCES, 'fixture read denied')
                return original(filename, *args, **kwargs)
            fault = patch('builtins.open', side_effect=failing_read)
        else:
            fault, hits = self.fail_write(store, mode)
        caught = None
        with fault:
            try:
                self.reopen(store)
            except OSError as exc:
                caught = exc
        intact = path.exists() and path.read_bytes() == before
        backups = list(self.data.glob(path.name + '.corrupt_*'))
        observations.append({'case': kind + ('_migration' if migration else '_load'),
                             'fault': mode, 'fault_hits': len(hits),
                             'error_propagated': caught is not None,
                             'original_file_unchanged': intact, 'corrupt_backups_created': len(backups)})
        self.assertGreaterEqual(len(hits), 1)
        self.assertIsNotNone(caught, 'I/O failure must be reported instead of resetting valid data')
        self.assertTrue(intact, 'I/O failure must preserve the valid original JSON byte-for-byte')
        self.assertEqual(backups, [], 'I/O failure is not evidence that JSON is corrupt')

    def test_announcement_read_error_preserves_valid_json(self):
        self.assert_load_io_preserves_file('announcement', 'read')

    def test_template_read_error_preserves_valid_json(self):
        self.assert_load_io_preserves_file('template', 'read')

    def test_template_migration_open_failure_preserves_valid_json(self):
        self.assert_load_io_preserves_file('template', 'open_write', migration=True)

    def test_template_migration_replace_failure_preserves_valid_json(self):
        self.assert_load_io_preserves_file('template', 'replace', migration=True)

    def test_template_migration_success_preserves_custom_entries(self):
        store, item_id, group_id = self.template()
        path = Path(store._path)
        doc = json.loads(path.read_text(encoding='utf-8'))
        doc['version'] = 2
        doc['templates'][-1]['price'] = 17
        path.write_text(json.dumps(doc, ensure_ascii=False), encoding='utf-8')
        reopened = self.reopen(store)
        self.assertEqual(reopened.get_template(item_id)['price'], 40)
        self.assertIn(group_id, [group['id'] for group in reopened.list_groups()])
        self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['version'], 3)
        self.assertEqual(snapshot(reopened), disk_snapshot(reopened))

    def test_invalid_json_still_recovers_using_existing_behavior(self):
        announcement = settings_store.AnnouncementStore(str(self.data))
        Path(announcement._path).write_text('{', encoding='utf-8')
        self.assertEqual(snapshot(self.reopen(announcement)), [])
        store, _, _ = self.template()
        path = Path(store._path)
        path.write_text('{', encoding='utf-8')
        reopened = self.reopen(store)
        self.assertTrue(reopened.list_templates())
        self.assertEqual(len(list(self.data.glob('templates.json.corrupt_*'))), 1)
        self.assertEqual(snapshot(reopened), disk_snapshot(reopened))

    def asset_store(self, *, primary='', fallback='local:missing.jpg', leading_blank=False):
        store, item_id, _ = self.template()
        item = store.get_template(item_id)
        item['cover'] = fallback
        item['covers'] = ([''] if leading_blank else []) + [primary or fallback]
        # Only the selected fixture participates in placeholder generation.
        store._templates = [item]
        store._save_locked()
        root = Path(templates_store.covers_dir())
        return store, item_id, root

    def asset_bytes(self, root):
        return {file.name: file.read_bytes() for file in root.iterdir() if file.is_file()}

    def assert_valid_primary_is_untouched(self, leading_blank=False):
        name = 'fixture_original_v1.jpg' if not leading_blank else 'actual-primary.jpg'
        store, item_id, root = self.asset_store(primary='local:' + name, leading_blank=leading_blank)
        (root / name).write_bytes(b'original-primary-image')
        before = snapshot(store)
        original_assets = self.asset_bytes(root)
        with patch.object(store, '_save_locked', side_effect=OSError('fixture JSON write failed')) as save:
            caught = None
            try:
                store.ensure_placeholder_covers()
            except OSError as exc:
                caught = exc
        unchanged = self.asset_bytes(root) == original_assets
        observations.append({'case': 'valid_primary_asset', 'leading_blank': leading_blank,
                             'assets_unchanged': unchanged, 'metadata_unchanged': snapshot(store) == before,
                             'json_write_attempted': save.call_count})
        self.assertTrue(unchanged, 'a missing fallback must not overwrite the real public primary image')
        self.assertEqual(snapshot(store), before)
        self.assertIsNone(caught, 'valid primary means no placeholder transaction is needed')
        save.assert_not_called()

    def test_placeholder_valid_primary_ignores_missing_fallback_and_json_fault(self):
        self.assert_valid_primary_is_untouched()

    def test_placeholder_first_nonempty_primary_ignores_missing_fallback(self):
        self.assert_valid_primary_is_untouched(leading_blank=True)

    def shared_asset_store(self):
        store, item_id, root = self.asset_store()
        shared = root / (item_id + '_v1.jpg')
        shared.write_bytes(b'other-template-shared-original')
        other = store.create_template({'id': 'fixture_shared', 'group_id': 'restore', 'name': '共享图模板',
                                       'prompt': '保持图片', 'cover': 'local:' + shared.name})
        return store, item_id, root, shared, other

    def test_placeholder_shared_existing_asset_is_not_overwritten_on_success(self):
        store, item_id, root, shared, other = self.shared_asset_store()
        import builtins
        original = builtins.open
        modes = []
        def observe_open(filename, *args, **kwargs):
            mode = args[0] if args else kwargs.get('mode', 'r')
            if isinstance(filename, (str, bytes, os.PathLike)) and Path(filename).parent == root and any(flag in mode for flag in ('w', 'x')):
                modes.append(mode)
            return original(filename, *args, **kwargs)
        with patch('builtins.open', side_effect=observe_open):
            store.ensure_placeholder_covers()
        self.assertEqual(shared.read_bytes(), b'other-template-shared-original')
        changed = store.get_template(item_id)
        self.assertNotEqual(changed['cover'], 'local:' + shared.name)
        self.assertEqual(store.get_template(other['id'])['cover'], other['cover'])
        self.assertTrue((root / changed['cover'][6:]).is_file())
        self.assertTrue(modes)
        self.assertTrue(all('x' in mode for mode in modes), 'placeholder assets must be created exclusively')
        self.assertEqual(snapshot(store), disk_snapshot(store))

    def test_placeholder_shared_existing_asset_is_not_overwritten_on_json_failure(self):
        store, _, root, shared, _ = self.shared_asset_store()
        before = snapshot(store)
        original_assets = self.asset_bytes(root)
        fault, _ = self.fail_write(store, 'replace')
        with fault:
            with self.assertRaises(OSError):
                store.ensure_placeholder_covers()
        self.assertEqual(shared.read_bytes(), b'other-template-shared-original')
        self.assertEqual(self.asset_bytes(root), original_assets)
        self.assertEqual(snapshot(store), before)
        self.assertEqual(disk_snapshot(store), before)

    def assert_failed_placeholder_cleans_created_assets(self, mode):
        store, _, root = self.asset_store()
        original_assets = self.asset_bytes(root)
        before = snapshot(store)
        fault, _ = self.fail_write(store, mode)
        with fault:
            with self.assertRaises(OSError):
                store.ensure_placeholder_covers()
        self.assertEqual(self.asset_bytes(root), original_assets, 'rejected metadata must not leave newly created orphan assets')
        self.assertEqual(snapshot(store), before)
        self.assertEqual(disk_snapshot(store), before)

    def test_placeholder_json_open_failure_removes_new_assets(self):
        self.assert_failed_placeholder_cleans_created_assets('open_write')

    def test_placeholder_json_replace_failure_removes_new_assets(self):
        self.assert_failed_placeholder_cleans_created_assets('replace')

    def test_placeholder_partial_asset_write_failure_removes_only_its_new_file(self):
        store, _, root = self.asset_store()
        retained = root / 'retained.jpg'
        retained.write_bytes(b'keep-existing-image')
        original_assets = self.asset_bytes(root)
        before = snapshot(store)
        import builtins
        original = builtins.open
        class BrokenWriter:
            def __init__(self, file): self.file = file
            def __enter__(self): return self
            def __exit__(self, *args): self.file.close()
            def write(self, data):
                self.file.write(data[:3])
                self.file.flush()
                raise OSError(errno.ENOSPC, 'fixture partial asset write failed')
        def fail_asset_write(filename, *args, **kwargs):
            mode = args[0] if args else kwargs.get('mode', 'r')
            file = original(filename, *args, **kwargs)
            if isinstance(filename, (str, bytes, os.PathLike)) and Path(filename).parent == root and any(flag in mode for flag in ('w', 'x')):
                return BrokenWriter(file)
            return file
        with patch('builtins.open', side_effect=fail_asset_write):
            store.ensure_placeholder_covers()
        self.assertEqual(self.asset_bytes(root), original_assets)
        self.assertEqual(snapshot(store), before)


OPERATIONS = ('announcement_create', 'announcement_update', 'announcement_delete',
              'group_create', 'group_update', 'group_delete', 'template_create',
              'template_update', 'template_delete', 'template_inc_usage', 'template_reprice',
              'cover_set', 'cover_legacy_set', 'cover_delete', 'cover_placeholder')


def failure_case(name, mode):
    def test(self):
        self.assert_atomic_failure(name, mode)
    return test


def success_case(name):
    def test(self):
        self.assert_success_persists(name)
    return test


for operation in OPERATIONS:
    for fault in ('open_write', 'replace'):
        setattr(SequentialStoreTests, 'test_%s_%s_failure_is_atomic' % (operation, fault), failure_case(operation, fault))
    setattr(SequentialStoreTests, 'test_%s_success_persists' % operation, success_case(operation))


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(SequentialStoreTests)
    names = [test._testMethodName for test in suite]
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    failed = {test._testMethodName for test, _ in result.failures + result.errors}
    output = {'cases': [{'case': name, 'passed': name not in failed} for name in names],
              'observations': observations, 'summary': {'total': result.testsRun,
              'passed': result.testsRun - len(failed), 'failed': len(failed),
              'harness_errors': len(result.errors)}}
    (OUTPUT / 'sequential_store_results.json').write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding='utf-8')
    fixture.cleanup()
    print('SEQUENTIAL_STORE_SUMMARY total=%d passed=%d failed=%d harness_errors=%d' %
          (result.testsRun, result.testsRun - len(failed), len(failed), len(result.errors)), flush=True)
    raise SystemExit(0 if result.wasSuccessful() else 1)
