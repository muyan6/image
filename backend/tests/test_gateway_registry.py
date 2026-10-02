"""Offline named-gateway settings/API migration, masking and transaction checks."""
import copy
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(os.environ.get('REVIEW_ROOT',Path(__file__).resolve().parents[2])).resolve()
OUTPUT=Path(os.environ.get('REVIEW_OUTPUT',ROOT/'audit/gateway-registry-tests')).resolve();OUTPUT.mkdir(parents=True,exist_ok=True)
fixture=tempfile.TemporaryDirectory(prefix='gateway_registry_',dir=OUTPUT)
SOURCE=Path(fixture.name)/'backend';SOURCE.mkdir()
for file in (ROOT/'backend').glob('*.py'):shutil.copy2(file,SOURCE/file.name)
sys.path.insert(0,str(SOURCE))
from settings_store import SettingsStore,DEFAULT_SETTINGS
from gateway_registry import gateway_defaults,MAX_GATEWAYS
from gateway_profiles import gateway_profile
import admin_api
from fastapi import FastAPI
from fastapi.testclient import TestClient

GW='gw_'+'1'*32;GW2='gw_'+'2'*32
def configured(name='备用网关',key='fixture-secondary-2222'):
    return {**gateway_defaults(GW),'name':name,'base_url':'https://fixture.invalid','api_key':key,
        'model_light':'fixture-light','model_fine':'fixture-fine','timeout':90,
        'price_light_cny':.1,'price_fine_cny':.2}


class GatewayRegistryTests(unittest.TestCase):
    def setUp(self):
        self.data=Path(fixture.name)/self._testMethodName;self.data.mkdir()
        self.settings=SettingsStore(str(self.data))
        self.environment=patch.dict(os.environ,ADMIN_PASSWORD='fixture-registry-admin');self.environment.start()
        app=FastAPI();app.include_router(admin_api.make_admin_router(settings=self.settings,
            announcements=SimpleNamespace(),templates=SimpleNamespace(reprice_by_engine=lambda _:None),
            jobs=SimpleNamespace(),users=SimpleNamespace(),health_fn=lambda:{},stats_fn=lambda:{'fixture':True}))
        self.client=TestClient(app);login=self.client.post('/admin/api/login',json={'password':'fixture-registry-admin'})
        self.assertEqual(login.status_code,200);self.client.headers.update({'X-Admin-Request':'1'})
    def tearDown(self):self.client.close();self.environment.stop()
    def two(self):
        self.settings.update({'providers':{'worldcodes':{'api_key':'fixture-primary-1111'},GW:configured()},'chain':['worldcodes',GW]})
    def legacy(self,text=None):
        doc=copy.deepcopy(DEFAULT_SETTINGS);doc.pop('gateway_registry_revision',None)
        doc['providers']['worldcodes'].pop('request_mode',None);doc['providers']['worldcodes'].pop('async_endpoint',None)
        doc['providers']['worldcodes'].pop('name',None)
        doc['providers']['worldcodes'].update(api_key='fixture-original-1111',tiers={'light':{'base_url':'https://old-light.invalid',
            'api_key':'fixture-light-3333','price_cny':.23},'fine':{'api_key':'fixture-fine-4444'}})
        doc['providers'].update(fal={'enabled':True,'api_key':'fixture-retired-fal'},
            baidu={'enabled':True,'api_key':'fixture-retired-baidu','secret_key':'fixture-retired-secret'},local={'enabled':True})
        doc['chain']=['fal','worldcodes','local','baidu']
        doc['text_generation']={'enabled':True,'model':'fixture-text','endpoint':'/v1/images/generations','price':40} if text is None else text
        return doc

    def test_fresh_defaults_only_primary_gateway_and_retired_always_disabled(self):
        doc=self.settings.snapshot();self.assertEqual(set(doc['providers']),{'worldcodes'})
        self.assertEqual(doc['chain'],['worldcodes']);self.assertEqual(doc['providers']['worldcodes']['name'],'主网关')
        self.assertEqual(self.settings.gateway_for('light')['request_mode'],'async')
        self.assertEqual(self.settings.gateway_candidates(),[])
        for retired in ('fal','baidu','local'):self.assertFalse(self.settings.provider_enabled(retired))

    def test_priority_names_tiers_candidates_and_detached_metadata(self):
        self.two();self.settings.update({'chain':[GW,'worldcodes']})
        candidates=self.settings.gateway_candidates('light');self.assertEqual([c['gateway_id'] for c in candidates],[GW,'worldcodes'])
        self.assertEqual(candidates[0]['gateway_name'],'备用网关');self.assertEqual(candidates[0]['request_timeout'],90)
        self.assertEqual(candidates[0]['request_mode'],'sync')
        candidates[0]['api_key']='caller mutation';self.assertNotEqual(self.settings.gateway_for('light',GW)['api_key'],'caller mutation')
        self.settings.update({'providers':{GW:{'tiers':{'fine':{'enabled':False}}}}})
        self.assertEqual(self.settings.gateway_for('fine')['gateway_id'],'worldcodes')
        explicit=self.settings.gateway_for('fine',GW);self.assertFalse(explicit['enabled']);self.assertEqual(explicit['api_key'],'fixture-secondary-2222')

    def test_disabled_explicit_lookup_and_deleted_primary_never_fallback_or_resurrect(self):
        self.two();self.settings.update({'providers':{GW:{'enabled':False}}})
        self.assertEqual(self.settings.gateway_for('light',GW)['gateway_id'],GW)
        self.assertEqual(self.settings.gateway_for('light',GW)['api_key'],'fixture-secondary-2222')
        self.settings.update({'providers':{'worldcodes':None},'chain':[GW]})
        self.assertEqual(self.settings.gateway_for('light','worldcodes'),{})
        reopened=SettingsStore(str(self.data));self.assertEqual(set(reopened.snapshot()['providers']),{GW})
        self.assertEqual(reopened.gateway_for('light','worldcodes'),{})

    def test_all_disabled_valid_empty_chain_or_registry_invalid(self):
        self.two();self.settings.update({'providers':{'worldcodes':{'enabled':False},GW:{'enabled':False}}})
        self.assertEqual(self.settings.gateway_candidates('light'),[])
        self.assertEqual(self.settings.gateway_for('light')['gateway_id'],'worldcodes')
        before=self.settings.snapshot()
        for changes in ({'chain':[]},{'providers':None},{'providers':{'worldcodes':None,GW:None},'chain':[]}):
            with self.assertRaises(ValueError):self.settings.update(changes)
            self.assertEqual(self.settings.snapshot(),before)

    def test_stable_ids_exact_chain_and_twenty_gateway_bound(self):
        self.two();before=self.settings.snapshot()
        for change in ({'chain':['worldcodes','worldcodes']},{'chain':['worldcodes']},{'chain':['worldcodes',GW,GW2]},
                       {'providers':{'gateway_bad':configured()},'chain':['worldcodes',GW,'gateway_bad']},
                       {'providers':{GW2:configured()}}):
            with self.assertRaises(ValueError):self.settings.update(change)
            self.assertEqual(self.settings.snapshot(),before)
        providers={'gw_'+format(i,'032x'):configured() for i in range(2,MAX_GATEWAYS+2)}
        with self.assertRaises(ValueError):self.settings.update({'providers':providers,'chain':['worldcodes',GW,*providers]})

    def test_field_modes_urls_lengths_and_nonfinite_costs_reject_atomically(self):
        self.two();before=self.settings.snapshot()
        bads=[{'name':''},{'name':'x'*41},{'enabled':1},{'base_url':'https://user:secret@host.invalid'},
              {'base_url':'https://host.invalid?token=secret'},{'base_url':'https://host.invalid#fragment'},
              {'api_key':3},{'api_key':'x'*8193},{'timeout':True},{'timeout':601},{'model_fine':'x'*201},
              {'price_light_cny':float('nan')},{'price_fine_cny':float('inf')},{'price_fine_cny':True},
              {'endpoint':'//other.invalid'},{'endpoint':'/edits?token=secret'},{'request_mode':'automatic'},
              {'async_endpoint':'/edits#fragment'},{'async_endpoint':'/a b'},{'async_endpoint':'x'*201}]
        for bad in bads:
            with self.assertRaises(ValueError):self.settings.update({'providers':{GW:bad},'prompts':{'light':'must-not-publish'}})
            self.assertEqual(self.settings.snapshot(),before)
        for bad in ({'enabled':'yes'},{'price_cny':float('nan')},{'async_endpoint':'//bad'}, {'request_mode':'wrong'}):
            with self.assertRaises(ValueError):self.settings.update({'providers':{GW:{'tiers':{'fine':bad}}}})

    def test_sync_async_fields_inherit_override_and_explicit_blanks_do_not_borrow(self):
        self.two();self.settings.update({'providers':{GW:{'request_mode':'sync','async_endpoint':'/jobs/create',
            'tiers':{'fine':{'request_mode':'async','async_endpoint':'/custom/async','api_key':''}}}}})
        fine=self.settings.gateway_for('fine',GW);self.assertEqual(fine['request_mode'],'async')
        self.assertEqual(fine['async_endpoint'],'/custom/async');self.assertEqual(fine['api_key'],'')
        self.assertEqual(self.settings.gateway_for('light',GW)['request_mode'],'sync')
        self.assertEqual(self.settings.gateway_for('fine')['gateway_id'],'worldcodes')

    def test_dynamic_mask_unmask_roundtrip_clear_and_fresh_key(self):
        self.two();self.settings.update({'text_generation':{'api_key':'fixture-text-9999'},'providers':{GW:{'tiers':{
            'light':{'api_key':'fixture-light-3333'},'fine':{'api_key':'fixture-fine-4444'}}}}})
        response=self.client.get('/admin/api/settings');self.assertEqual(response.status_code,200)
        doc=response.json();self.assertNotIn('fixture-secondary-2222',response.text);self.assertNotIn('fixture-fine-4444',response.text)
        self.assertEqual(doc['providers'][GW]['api_key'],'••••2222');self.assertEqual(doc['providers'][GW]['tiers']['fine']['api_key'],'••••4444')
        doc['providers'][GW]['name']='重命名';saved=self.client.put('/admin/api/settings',json={'providers':doc['providers'],'chain':doc['chain']})
        self.assertEqual(saved.status_code,200,saved.text);self.assertEqual(self.settings.gateway_for('fine',GW)['api_key'],'fixture-fine-4444')
        saved=self.client.put('/admin/api/settings',json={'providers':{GW:{'api_key':None,'tiers':{'fine':{'api_key':''},'light':{'api_key':'new-fixture-5555'}}}}})
        self.assertEqual(saved.status_code,200,saved.text)
        self.assertEqual(self.settings.provider(GW)['api_key'],'fixture-secondary-2222')
        self.assertEqual(self.settings.gateway_for('fine',GW)['api_key'],'');self.assertEqual(self.settings.gateway_for('light',GW)['api_key'],'new-fixture-5555')
        self.assertEqual(self.settings.text_generation()['api_key'],'fixture-text-9999')

    def test_first_masked_inherited_tier_key_keeps_original_primary_and_new_ids_do_not_borrow(self):
        self.two();tiers=self.client.get('/admin/api/settings').json()['providers'][GW]['tiers']
        self.assertEqual(tiers['light']['api_key'],'••••2222')
        response=self.client.put('/admin/api/settings',json={'providers':{GW:{'tiers':tiers}}})
        self.assertEqual(response.status_code,200);self.assertEqual(self.settings.gateway_for('fine',GW)['api_key'],'fixture-secondary-2222')
        masked=admin_api._unmask_secrets({'providers':{GW2:{'api_key':'••••2222','tiers':{'fine':{'api_key':'••••1111'}}}}},self.settings.snapshot())
        self.assertEqual(masked['providers'][GW2]['api_key'],'');self.assertEqual(masked['providers'][GW2]['tiers']['fine']['api_key'],'')

    def test_overview_dynamic_flags_contains_no_retired_status_or_gateway_secrets(self):
        self.two();self.settings.update({'providers':{GW:{'enabled':False}}})
        response=self.client.get('/admin/api/overview');self.assertEqual(response.status_code,200,response.text);doc=response.json()
        self.assertEqual(set(doc['providers']),{'worldcodes',GW});self.assertEqual(set(doc['configured']),{'worldcodes',GW})
        self.assertTrue(doc['configured'][GW]);self.assertFalse(doc['providers'][GW]['light_ready']);self.assertFalse(doc['providers'][GW]['enabled'])
        self.assertNotIn('fixture-secondary',response.text);self.assertNotIn('api_key',response.text)
        no_header=self.client.get('/admin/api/settings',headers={'X-Admin-Request':''});self.assertEqual(no_header.status_code,403)

    def test_legacy_exact_backup_retired_cleanup_and_text_light_snapshot_precede_migration(self):
        legacy=self.legacy();raw=json.dumps(legacy,ensure_ascii=False,indent=3).encode();Path(self.settings._path).write_bytes(raw)
        migrated=SettingsStore(str(self.data));doc=migrated.snapshot()
        backups=list(self.data.glob('settings.json.gateway-registry-backup-*'));self.assertEqual(len(backups),1);self.assertEqual(backups[0].read_bytes(),raw)
        self.assertEqual(set(doc['providers']),{'worldcodes'});self.assertEqual(doc['chain'],['worldcodes'])
        self.assertEqual(doc['providers']['worldcodes']['api_key'],'fixture-original-1111')
        self.assertEqual(doc['providers']['worldcodes']['tiers'],legacy['providers']['worldcodes']['tiers'])
        self.assertEqual(doc['providers']['worldcodes']['request_mode'],'async')
        text=migrated.text_generation();self.assertEqual(text['api_key'],'fixture-light-3333');self.assertEqual(text['base_url'],'https://old-light.invalid')
        self.assertEqual(text['price_cny'],.23)
        self.assertEqual(SettingsStore(str(self.data)).snapshot(),doc);self.assertEqual(len(list(self.data.glob('settings.json.gateway-registry-backup-*'))),1)

    def test_explicit_text_connection_remains_independent_of_gateway_edit_reorder_delete(self):
        self.two();self.settings.update({'text_generation':{'base_url':'https://text.invalid','api_key':'fixture-text','timeout':41,'price_cny':.31}})
        text=self.settings.text_generation()
        self.settings.update({'providers':{'worldcodes':None,GW:{'base_url':'https://changed.invalid','api_key':'fixture-changed'}},'chain':[GW]})
        self.assertEqual(self.settings.text_generation(),text);self.assertEqual(SettingsStore(str(self.data)).text_generation(),text)

    def test_migration_write_failure_preserves_file_and_existing_memory_and_keeps_backup(self):
        before=self.settings.snapshot();raw=json.dumps(self.legacy()).encode();Path(self.settings._path).write_bytes(raw)
        with patch.object(self.settings,'_save_locked',side_effect=OSError('FIXTURE_WRITE_FAILURE')):
            with self.assertRaises(OSError):self.settings._load_or_init(copy.deepcopy(DEFAULT_SETTINGS))
        self.assertEqual(self.settings.snapshot(),before);self.assertEqual(Path(self.settings._path).read_bytes(),raw)
        self.assertTrue(any(p.read_bytes()==raw for p in self.data.glob('settings.json.gateway-registry-backup-*')))

    def test_atomic_settings_put_rejects_invalid_chain_and_failed_write_publishes_nothing(self):
        self.two();before=self.settings.snapshot();raw=Path(self.settings._path).read_bytes()
        reply=self.client.put('/admin/api/settings',json={'providers':{GW:None},'chain':[],'prompts':{'light':'must-not-publish'}})
        self.assertEqual(reply.status_code,400);self.assertEqual(self.settings.snapshot(),before)
        with patch.object(self.settings,'_save_locked',side_effect=OSError('FIXTURE_WRITE_FAILURE')):
            with self.assertRaises(OSError):self.settings.update({'providers':{GW:{'name':'not-published'}}})
        self.assertEqual(self.settings.snapshot(),before);self.assertEqual(Path(self.settings._path).read_bytes(),raw)

    def test_real_replace_failure_keeps_prior_file_and_memory(self):
        self.two();before=self.settings.snapshot();raw=Path(self.settings._path).read_bytes()
        with patch('settings_store.os.replace',side_effect=PermissionError('FIXTURE_REPLACE_FAILURE')):
            with self.assertRaises(PermissionError):self.settings.update({'providers':{GW:{'name':'not-published'}}})
        self.assertEqual(self.settings.snapshot(),before);self.assertEqual(Path(self.settings._path).read_bytes(),raw)

    def test_backup_creation_failure_stops_migration_before_source_write(self):
        before=self.settings.snapshot();raw=json.dumps(self.legacy()).encode();Path(self.settings._path).write_bytes(raw)
        original_open=open
        def guarded(file,*args,**kwargs):
            if '.gateway-registry-backup-' in str(file):raise PermissionError('FIXTURE_BACKUP_FAILURE')
            return original_open(file,*args,**kwargs)
        with patch('builtins.open',side_effect=guarded):
            with self.assertRaises(PermissionError):self.settings._load_or_init(copy.deepcopy(DEFAULT_SETTINGS))
        self.assertEqual(self.settings.snapshot(),before);self.assertEqual(Path(self.settings._path).read_bytes(),raw)

    def test_huge_integer_cost_is_http_400_and_does_not_publish_memory_or_disk(self):
        self.two();before=self.settings.snapshot();raw=Path(self.settings._path).read_bytes()
        for conf in ({'price_light_cny':10**400},{'tiers':{'fine':{'price_cny':10**400}}}):
            response=self.client.put('/admin/api/settings',json={'providers':{GW:conf},'prompts':{'light':'must-not-publish'}})
            self.assertEqual(response.status_code,400,response.text)
            self.assertEqual(self.settings.snapshot(),before);self.assertEqual(Path(self.settings._path).read_bytes(),raw)


if __name__=='__main__':
    names=[n for n in GatewayRegistryTests.__dict__ if n.startswith('test_')]
    result=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(GatewayRegistryTests(n) for n in names))
    failed={t._testMethodName for t,_ in result.failures+result.errors}
    (OUTPUT/'gateway_registry_results.json').write_text(json.dumps({'cases':[{'case':n,'passed':n not in failed} for n in names]},ensure_ascii=False,indent=2),encoding='utf-8')
    fixture.cleanup();print('GATEWAY_REGISTRY_SUMMARY total=%d passed=%d failed=%d'%(result.testsRun,result.testsRun-len(failed),len(failed)))
    raise SystemExit(0 if result.wasSuccessful() else 1)
