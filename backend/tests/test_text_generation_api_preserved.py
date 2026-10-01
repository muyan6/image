"""The frontend-only removal leaves the configured backend API operational."""
import json
import unittest
from test_cloud_pipeline import CloudTests, m, OUTPUT, initial_connections


class PreservedApiTests(CloudTests):
    def setUp(self):
        super().setUp()
        m.settings.update({'text_generation':{'enabled':True,'model':'fixture-model','price':40,
            'base_url':'https://fixture.invalid','api_key':'fixture','price_cny':.1}})

    def test_configured_backend_still_accepts_authenticated_text_jobs(self):
        result=self.client.post('/api/text-generation',headers=self.headers,
                                json={'prompt':'fixture landscape','aspect_ratio':'2:3'})
        self.assertEqual(result.status_code,200,result.text)
        job=m.jobs.get(result.json()['job_id'])
        self.assertEqual(job['input_mode'],'text');self.assertEqual(job['cloud_phase'],'queued')
        self.assertEqual(job['cloud_request']['model'],'fixture-model')
        self.assertEqual(m.users.get_balance('sample_user'),60)

    def test_backend_authentication_is_still_required(self):
        self.assertEqual(self.client.post('/api/text-generation',json={'prompt':'fixture'}).status_code,401)

    def test_backend_enabled_configuration_is_not_changed_by_frontend_removal(self):
        result=self.client.get('/api/config').json()
        self.assertTrue(result['text_generation']['ready'])
        self.assertEqual(result['text_generation']['price'],40)


if __name__=='__main__':
    names=[n for n in PreservedApiTests.__dict__ if n.startswith('test_')]
    result=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(PreservedApiTests(n) for n in names))
    failed={t._testMethodName for t,_ in result.failures+result.errors}
    (OUTPUT/'text_generation_api_preserved_results.json').write_text(json.dumps(
        {'cases':[{'case':n,'passed':n not in failed} for n in names]},indent=2),encoding='utf-8')
    for c in initial_connections:
        try:c.close()
        except Exception:pass
    print('TEXT_API_PRESERVED_SUMMARY total=%d passed=%d failed=%d'%(result.testsRun,result.testsRun-len(failed),len(failed)))
    raise SystemExit(0 if result.wasSuccessful() else 1)
