"""Template tier, price and model consistency with isolated billing data."""
import copy,json,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from test_workflow import WorkflowTests,m,OUTPUT,initial_connections
from template_output import select_template_quality

class TierTests(WorkflowTests):
    def template(self):return {'id':'poster','name':'拼贴','engine':'fine','price':999,
        'prompt':'保留主体，水彩风格','model_override':'old-4k','gateway_size':'4096x4096','output_size':4096,'text_fields':[]}

    def test_tier_user_choice_overrides_template_default(self):
        self.assertEqual(m._chosen_quality('light','fine'),'light')
        self.assertEqual(m._chosen_quality('fine','light'),'fine')
        self.assertEqual(m._chosen_quality('','fine'),'light')
        with self.assertRaises(m.HTTPException):m._chosen_quality('invalid','fine')

    def test_tier_snapshot_preserves_style_but_uses_global_models(self):
        raw=self.template();original=copy.deepcopy(raw)
        for q in ('light','fine'):
            t=select_template_quality(raw,q)
            self.assertEqual(t['engine'],q);self.assertEqual(t['prompt'],raw['prompt'])
            self.assertEqual(t['model_override'],'');self.assertEqual(t['gateway_size'],'');self.assertEqual(t['output_size'],0)
        self.assertEqual(raw,original)

    def test_tier_both_routes_charge_same_prices_as_restore(self):
        m.settings.update({'prices':{'light':20,'fine':80}});raw=self.template();sent=[]
        with patch.object(m,'_resolve_template',return_value=(raw,'fine',{})), \
             patch.object(m,'_get_client',return_value=SimpleNamespace(configured=True)), \
             patch.object(m.pool,'submit',side_effect=lambda *a:sent.append(a)):
            low=self.client.post('/api/rescue',headers=self.headers,data={'quality':'light','template_id':'poster'},files={'image':('a.jpg',self.image(),'image/jpeg')})
            self.assertEqual(low.status_code,200,low.text);self.assertEqual(low.json()['price'],20);self.assertEqual(low.json()['quality'],'light')
            m._uploads['fixture_upload']={'openid':'sample_user','key':'incoming/a.jpg','created_at':m.time.time(),'ext':'.jpg'}
            with patch.object(m,'cos_head',return_value=True),patch.object(m,'cos_get',return_value=self.image()):
                high=self.client.post('/api/rescue/by-upload',headers=self.headers,json={'quality':'fine','template_id':'poster','upload_id':'fixture_upload'})
            self.assertEqual(high.status_code,200,high.text);self.assertEqual(high.json()['price'],80);self.assertEqual(high.json()['quality'],'fine')
        self.assertEqual(m.users.get_balance('sample_user'),0)
        self.assertEqual([a[2] for a in sent],['light','fine'])
        self.assertTrue(all(a[4]['model_override']=='' for a in sent))

    def test_tier_price_ignores_old_template_override_and_allows_zero(self):
        m.settings.update({'prices':{'light':0,'fine':80}})
        self.assertEqual(m._effective_price('light',self.template()),0)
        self.assertEqual(m._effective_price('fine',self.template()),80)
        # Internal context used by text-to-image violation billing is not a catalog template.
        self.assertEqual(m._effective_price('light',{'price':75}),75)

    def test_tier_capability_and_public_catalog_prices(self):
        self.assertEqual(self.client.get('/api/config').json()['template_quality_options'],['light','fine'])
        for t in self.client.get('/api/templates').json()['items']:
            self.assertEqual(t['quality_options'],['light','fine']);self.assertEqual(t['tier_prices'],m.settings.prices())

if __name__=='__main__':
    suite=unittest.TestSuite(TierTests(n) for n in TierTests.__dict__ if n.startswith('test_tier_'))
    names=[t._testMethodName for t in suite];result=unittest.TextTestRunner(verbosity=2).run(suite)
    failed={t._testMethodName for t,_ in result.failures+result.errors}
    (OUTPUT/'template_tiers_results.json').write_text(json.dumps({'cases':[{'case':n,'passed':n not in failed} for n in names]},indent=2),encoding='utf-8')
    for c in initial_connections:
        try:c.close()
        except Exception:pass
    print('TEMPLATE_TIERS_SUMMARY total=%d passed=%d failed=%d'%(result.testsRun,result.testsRun-len(failed),len(failed)))
    raise SystemExit(0 if result.wasSuccessful() else 1)
