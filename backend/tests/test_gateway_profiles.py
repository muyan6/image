"""Tier configuration/masking/routing regressions; isolated data and no network."""
import json,unittest
from pathlib import Path
from unittest.mock import patch
from test_workflow import WorkflowTests,m,OUTPUT,initial_connections
from gateway_profiles import gateway_profile,text_gateway
from gateway_async import AsyncImages
import cloud_pipeline as cp

class ProfileTests(WorkflowTests):
    def tearDown(self):
        if m.cloud._audits:
            m.cloud._audits.close();m.cloud._audits=None
        super().tearDown()

    def config_tiers(self):
        m.settings.update({'providers':{'worldcodes':{'enabled':True,'base_url':'https://legacy.invalid','api_key':'legacy-secret-7890',
            'tiers':{'light':{'base_url':'https://light.invalid','api_key':'light-secret-1234','endpoint':'/v2/edits','model':'light-model','timeout':60,'price_cny':.03},
                     'fine':{'base_url':'https://fine.invalid','api_key':'fine-secret-5678','endpoint':'/custom/edits','model':'fine-model','timeout':300,'price_cny':.2}}}}})
    def test_profile_old_shared_settings_remain_compatible(self):
        m.settings.update({'providers':{'worldcodes':{'api_key':'legacy-fixture','base_url':'https://legacy.invalid'}}})
        for q in ('light','fine'):
            c=m.settings.gateway_for(q);self.assertEqual(c['api_key'],'legacy-fixture')
            self.assertEqual(c['model_'+q],m.settings.provider('worldcodes')['model_'+q])
        self.assertEqual(m.settings.provider('worldcodes')['tiers'],{'light':{},'fine':{}})
    def test_profile_tiers_have_separate_all_connection_fields(self):
        self.config_tiers()
        a,b=m.settings.gateway_for('light'),m.settings.gateway_for('fine')
        for field in ('base_url','api_key','endpoint','timeout'):self.assertNotEqual(a[field],b[field])
        self.assertEqual(a['model_light'],'light-model');self.assertEqual(b['model_fine'],'fine-model')
        self.assertEqual(m.settings.prices(),{'light':40,'fine':40})
    def test_profile_clear_one_key_does_not_fallback_or_clear_other(self):
        self.config_tiers();m.settings.update({'providers':{'worldcodes':{'tiers':{'fine':{'api_key':''}}}}})
        self.assertEqual(m.settings.gateway_for('fine')['api_key'],'')
        self.assertEqual(m.settings.gateway_for('light')['api_key'],'light-secret-1234')
        self.assertEqual(m.settings.provider('worldcodes')['api_key'],'legacy-secret-7890')
    def test_profile_admin_masks_both_keys_and_preserves_masked_roundtrip(self):
        self.config_tiers();r=self.admin.get('/admin/api/settings');self.assertEqual(r.status_code,200)
        data=r.json();tiers=data['providers']['worldcodes']['tiers']
        self.assertEqual(tiers['light']['api_key'],'••••1234');self.assertEqual(tiers['fine']['api_key'],'••••5678')
        self.assertNotIn('light-secret',r.text);self.assertNotIn('fine-secret',r.text)
        tiers['fine']['model']='new-fine'
        saved=self.admin.put('/admin/api/settings',json={'providers':{'worldcodes':{'tiers':tiers}}})
        self.assertEqual(saved.status_code,200,saved.text)
        self.assertEqual(m.settings.gateway_for('fine')['api_key'],'fine-secret-5678')
        self.assertEqual(m.settings.gateway_for('light')['api_key'],'light-secret-1234')
    def test_profile_first_admin_save_inherits_masked_legacy_secret_without_loss(self):
        m.settings.update({'providers':{'worldcodes':{'api_key':'old-fixture-9876'}}})
        tiers=self.admin.get('/admin/api/settings').json()['providers']['worldcodes']['tiers']
        self.assertEqual(tiers['light']['api_key'],'••••9876')
        self.assertEqual(self.admin.put('/admin/api/settings',json={'providers':{'worldcodes':{'tiers':tiers}}}).status_code,200)
        for q in ('light','fine'):self.assertEqual(m.settings.gateway_for(q)['api_key'],'old-fixture-9876')
    def test_profile_admin_explicit_empty_key_is_durable(self):
        self.config_tiers()
        self.assertEqual(self.admin.put('/admin/api/settings',json={'providers':{'worldcodes':{'tiers':{'fine':{'api_key':''}}}}}).status_code,200)
        from settings_store import SettingsStore
        reopened=SettingsStore(str(self.d))
        self.assertEqual(reopened.gateway_for('fine')['api_key'],'')
        self.assertEqual(reopened.gateway_for('light')['api_key'],'light-secret-1234')
    def test_profile_client_cache_is_isolated_per_tier(self):
        self.config_tiers();a=m._get_client('worldcodes','light');b=m._get_client('worldcodes','fine')
        self.assertIsNot(a,b);self.assertEqual(a.base_url,'https://light.invalid');self.assertEqual(b.endpoint,'/custom/edits')
        self.assertEqual(a.api_key,'light-secret-1234');self.assertEqual(b.api_key,'fine-secret-5678')
        m.settings.update({'providers':{'worldcodes':{'tiers':{'fine':{'model':'other-fine'}}}}})
        self.assertIs(a,m._get_client('worldcodes','light'));self.assertIsNot(b,m._get_client('worldcodes','fine'))
    def test_profile_bad_url_or_path_rejected_atomically(self):
        self.config_tiers();before=m.settings.snapshot()
        for bad in [{'base_url':'https://user:key@host.invalid'}, {'base_url':'https://host.invalid?key=fixture'},
                    {'endpoint':'//other.invalid'}, {'endpoint':'/edits?key=fixture'}, {'api_key':12}, {'timeout':0}, {'price_cny':-1}]:
            r=self.admin.put('/admin/api/settings',json={'providers':{'worldcodes':{'tiers':{'fine':bad}}},'prompts':{'light':'should-not-save'}})
            self.assertEqual(r.status_code,400,r.text);self.assertEqual(m.settings.snapshot(),before)
    def test_profile_one_page_settings_put_saves_all_sections_together(self):
        self.config_tiers()
        p={'chain':['worldcodes','local'],'providers':{'worldcodes':{'tiers':{'fine':{'model':'new'}}}},
           'prompts':{'light':'自然优化'},'text_generation':{'enabled':True,'model':'text-model','price':50}}
        self.assertEqual(self.admin.put('/admin/api/settings',json=p).status_code,200)
        self.assertEqual(m.settings.chain(),p['chain']);self.assertEqual(m.settings.prompt_for('light'),'自然优化')
        self.assertEqual(m.settings.snapshot()['text_generation']['model'],'text-model')
        self.assertEqual(m.settings.gateway_for('fine')['model_fine'],'new')
    def test_profile_public_config_never_contains_private_connection(self):
        self.config_tiers()
        for path in ('/api/config','/api/health'):
            with patch.object(m.settings,'cos_ready',return_value=False):r=self.client.get(path)
            self.assertEqual(r.status_code,200)
            for value in ('light-secret','fine-secret','https://light.invalid','https://fine.invalid'):self.assertNotIn(value,r.text)
    def test_profile_cloud_uses_selected_tier_credentials_and_endpoint(self):
        self.config_tiers();m.settings.update({'cloud_pipeline':{'enabled':True},'tencent':{'secret_id':'fixture','secret_key':'fixture','cos_bucket':'fixture-123456'}})
        sent=[]
        def submit(client,model,prompt,url,size,endpoint):
            sent.append((client.base,client.conf['api_key'],model,endpoint));return 'imgtask_fixture'
        with patch.object(m.cloud,'ready',return_value=True),patch.object(AsyncImages,'submit',new=submit):
            for q in ('light','fine'):
                jid=m.cloud.admit('sample_user',quality=q,source={'key':'incoming/owned.jpg','ext':'.jpg'})['job_id']
                m.jobs.update(jid,cloud_phase='submit');m.cloud.step(jid)
                self.assertEqual(m.jobs.get(jid)['cloud_phase'],'generating')
                self.assertNotIn('api_key',m.jobs.get(jid)['cloud_request'])
        self.assertEqual(sent,[('https://light.invalid','light-secret-1234','light-model','/v2/edits'),
                               ('https://fine.invalid','fine-secret-5678','fine-model','/custom/edits')])
    def test_profile_cloud_poll_uses_same_tier_after_restart(self):
        self.config_tiers()
        with patch.object(m.cloud,'ready',return_value=True):jid=m.cloud.admit('sample_user',quality='fine',source={'key':'incoming/x.jpg','ext':'.jpg'})['job_id']
        m.jobs.update(jid,cloud_phase='generating',vendor_task_id='imgtask_fixture')
        seen=[]
        def poll(client,task):seen.append((client.base,client.conf['api_key']));return {'status':'running'}
        with patch.object(AsyncImages,'poll',new=poll):m.cloud.step(jid)
        self.assertEqual(seen,[('https://fine.invalid','fine-secret-5678')])
    def test_profile_cloud_fine_ready_does_not_require_light_credentials(self):
        self.config_tiers();m.settings.update({'providers':{'worldcodes':{'tiers':{'light':{'api_key':''}}}}})
        m.cloud.ready_cache=(None,0,False)
        with patch.object(m.settings,'cos_ready',return_value=True),patch.object(cp.cos,'origin_ready',return_value=True):
            self.assertFalse(m.cloud.ready('light'));self.assertTrue(m.cloud.ready('fine'))
    def test_profile_text_connection_is_independent(self):
        self.config_tiers()
        m.settings.update({'text_generation':{'enabled':True,'base_url':'https://text.invalid','api_key':'text-secret-9012','model':'text-model','timeout':45,'price_cny':0.12}})
        m.settings.update({'providers':{'worldcodes':{'tiers':{'light':{'base_url':'https://changed.invalid','api_key':''}}}}})
        conf=text_gateway(m.settings)
        self.assertEqual(conf['base_url'],'https://text.invalid');self.assertEqual(conf['api_key'],'text-secret-9012')
        self.assertEqual(conf['request_timeout'],45);self.assertEqual(conf['price_light_cny'],0.12)

    def test_profile_text_legacy_connection_is_migrated_once(self):
        from settings_store import SettingsStore
        self.config_tiers();doc=m.settings.snapshot()
        for key in ('base_url','api_key','timeout','price_cny'):doc['text_generation'].pop(key,None)
        doc['text_generation'].update(model='legacy-text',endpoint='/custom/generations',price=60)
        folder=self.d/'legacy-text';folder.mkdir()
        (folder/'settings.json').write_text(json.dumps(doc),encoding='utf-8')
        migrated=SettingsStore(str(folder));text=migrated.snapshot()['text_generation']
        self.assertEqual(text['base_url'],'https://light.invalid');self.assertEqual(text['api_key'],'light-secret-1234')
        self.assertEqual((text['model'],text['endpoint'],text['price']),('legacy-text','/custom/generations',60))
        migrated.update({'providers':{'worldcodes':{'tiers':{'light':{'api_key':'changed'}}}}})
        self.assertEqual(text_gateway(SettingsStore(str(folder)))['api_key'],'light-secret-1234')

    def test_profile_text_partial_connection_does_not_borrow_secret(self):
        from gateway_profiles import materialize_text_gateway
        doc={'providers':{'worldcodes':{'api_key':'do-not-copy','base_url':'https://light.invalid'}},'text_generation':{'base_url':'https://other.invalid'}}
        materialize_text_gateway(doc,dict(doc['text_generation']))
        self.assertEqual(doc['text_generation']['api_key'],'')

    def test_profile_text_mask_preserve_clear_and_public_redaction(self):
        self.config_tiers();m.settings.update({'text_generation':{'base_url':'https://text.invalid','api_key':'text-private-9012'}})
        masked=self.admin.get('/admin/api/settings').json()['text_generation']['api_key']
        self.assertEqual(masked,'••••9012')
        r=self.admin.put('/admin/api/settings',json={'text_generation':{'api_key':masked,'model':'new-text'}})
        self.assertEqual(r.status_code,200,r.text);self.assertEqual(text_gateway(m.settings)['api_key'],'text-private-9012')
        for path in ('/api/config','/api/health'):
            with patch.object(m.settings,'cos_ready',return_value=False):r=self.client.get(path)
            self.assertNotIn('text-private',r.text);self.assertNotIn('https://text.invalid',r.text)
        self.assertEqual(self.admin.put('/admin/api/settings',json={'text_generation':{'api_key':''}}).status_code,200)
        self.assertEqual(text_gateway(m.settings)['api_key'],'');self.assertEqual(m.settings.gateway_for('light')['api_key'],'light-secret-1234')

    def test_profile_text_validation_is_atomic(self):
        before=m.settings.snapshot()
        for bad in [{'base_url':'https://user:key@x.invalid'},{'base_url':'http://x.invalid'},
                    {'base_url':'https://x.invalid?key=secret'},{'endpoint':'//other.invalid'},
                    {'endpoint':'/generate?key=secret'},{'api_key':12},{'timeout':0},{'price_cny':-1}]:
            r=self.admin.put('/admin/api/settings',json={'text_generation':bad})
            self.assertEqual(r.status_code,400,r.text);self.assertEqual(m.settings.snapshot(),before)

    def test_profile_text_ready_does_not_require_photo_gateway(self):
        m.settings.update({'providers':{'worldcodes':{'enabled':False}},'text_generation':{'enabled':True,'model':'text-model','base_url':'https://text.invalid','api_key':'text-key'}})
        with patch.object(m.settings,'cos_ready',return_value=True),patch.object(cp.cos,'origin_ready',return_value=True),patch.object(m.settings,'gateway_for',side_effect=AssertionError('PHOTO_GATEWAY')):
            self.assertTrue(m.cloud.ready(text=True))
        m.settings.update({'text_generation':{'api_key':''}})
        with patch.object(m.settings,'cos_ready',return_value=True):self.assertFalse(m.cloud.ready(text=True))

    def test_profile_cloud_text_submit_and_poll_use_text_connection(self):
        self.config_tiers();m.settings.update({'text_generation':{'enabled':True,'model':'text-model','base_url':'https://text.invalid','api_key':'text-key','timeout':42,'price_cny':0.12}})
        with patch.object(m.cloud,'ready',return_value=True):
            jid=m.cloud.admit('sample_user',text={'prompt':'fixture','model':'text-model','endpoint':'/custom/generations','price':40})['job_id']
        seen=[]
        def submit(client,model,prompt,url,size,endpoint):
            seen.append((client.base,client.conf['api_key'],endpoint,client.conf['request_timeout']));return 'imgtask_text'
        with patch.object(AsyncImages,'submit',new=submit):m.jobs.update(jid,cloud_phase='submit');m.cloud.step(jid)
        self.assertEqual(seen,[('https://text.invalid','text-key','/custom/generations',42)])
        def poll(client,task):seen.append((client.base,client.conf['api_key']));return {'status':'running'}
        m.settings.update({'providers':{'worldcodes':{'tiers':{'light':{'api_key':'changed-light'}}}}})
        with patch.object(AsyncImages,'poll',new=poll):m.cloud.step(jid)
        self.assertEqual(seen[-1],('https://text.invalid','text-key'))
        self.assertEqual(m.jobs.get(jid)['cloud_request']['estimated_cost_cny'],0.12)

    def test_profile_text_timeout_reaches_actual_async_request(self):
        from test_cloud_origin import response
        import gateway_async
        m.settings.update({'text_generation':{'base_url':'https://text.invalid','api_key':'text-key','timeout':47}})
        r=response(200,b'{"status":"running"}')
        with patch.object(gateway_async.requests,'request',return_value=r) as request:
            AsyncImages(text_gateway(m.settings)).poll('imgtask_fixture')
        self.assertEqual(request.call_args.kwargs['timeout'],(5,47))
        self.assertEqual(request.call_args.kwargs['headers']['Authorization'],'Bearer text-key')

    def test_profile_text_reference_cost_does_not_change_credit_charge(self):
        m.settings.update({'text_generation':{'enabled':True,'model':'text-model','base_url':'https://text.invalid','api_key':'text-key','price_cny':0.12}})
        with patch.object(m.cloud,'ready',return_value=True):
            jid=m.cloud.admit('sample_user',text={'prompt':'fixture','model':'text-model','price':40})['job_id']
        m.jobs.update(jid,cloud_phase='finalize',vendor_result_key='images/fixture.png')
        with patch.object(cp.cos,'image_info',return_value={'width':1024,'height':1024}),patch.object(cp.cos,'object_metadata',return_value={'size':1000}),patch.object(cp.cos,'process_image',return_value={'size':1000}):
            m.cloud.step(jid)
        job=m.jobs.get(jid)
        self.assertEqual(job['status'],'succeeded',job.get('error'))
        self.assertEqual(job['cost_cny'],0.12);self.assertTrue(job['cost_estimated'])
        self.assertEqual(m.users.get_balance('sample_user'),60)

if __name__=='__main__':
    suite=unittest.TestSuite(ProfileTests(n) for n in ProfileTests.__dict__ if n.startswith('test_profile_'))
    names=[t._testMethodName for t in suite];result=unittest.TextTestRunner(verbosity=2).run(suite)
    failed={t._testMethodName for t,_ in result.failures+result.errors}
    (OUTPUT/'gateway_profiles_results.json').write_text(json.dumps({'cases':[{'case':n,'passed':n not in failed} for n in names]},indent=2),encoding='utf-8')
    for c in initial_connections:
        try:c.close()
        except Exception:pass
    print('GATEWAY_PROFILES_SUMMARY total=%d passed=%d failed=%d'%(result.testsRun,result.testsRun-len(failed),len(failed)))
    raise SystemExit(0 if result.wasSuccessful() else 1)
