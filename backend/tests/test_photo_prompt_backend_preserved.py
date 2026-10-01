"""Frontend directive removal does not alter independent backend contracts."""
import json,time,unittest
from unittest.mock import patch
from test_cloud_pipeline import CloudTests,m,OUTPUT,initial_connections

class BackendPromptTests(CloudTests):
    def test_backend_json_photo_endpoint_keeps_custom_prompt_contract(self):
        m._uploads['fixture']={**self.source,'openid':'sample_user','created_at':time.time()}
        response=self.client.post('/api/rescue/by-upload',headers=self.headers,
            json={'upload_id':'fixture','quality':'light','custom_prompt':'fixture lighting request'})
        self.assertEqual(response.status_code,200,response.text)
        self.assertIn('fixture lighting request',m.jobs.get(response.json()['job_id'])['cloud_request']['prompt'])

    def test_backend_multipart_contract_keeps_custom_prompt(self):
        m.settings.update({'cloud_pipeline':{'enabled':False}})
        with patch.object(m,'_moderate_or_reject',return_value=None),patch.object(m,'_moderate_text_or_reject',return_value=None), \
                patch.object(m,'_register_job',return_value={'code':0,'job_id':'abc123abc123'}) as register:
            response=self.client.post('/api/rescue',headers=self.headers,
                files={'image':('photo.jpg',self.image(),'image/jpeg')},data={'quality':'light','custom_prompt':'fixture lighting request'})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(register.call_args.kwargs['custom_prompt'],'fixture lighting request')

    def test_backend_still_requires_authentication(self):
        self.assertEqual(self.client.post('/api/rescue/by-upload',json={'upload_id':'fixture','custom_prompt':'fixture'}).status_code,401)

if __name__=='__main__':
    names=[n for n in BackendPromptTests.__dict__ if n.startswith('test_')]
    result=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(BackendPromptTests(n) for n in names))
    failed={t._testMethodName for t,_ in result.failures+result.errors}
    (OUTPUT/'photo_prompt_backend_preserved_results.json').write_text(json.dumps({'cases':[{'case':n,'passed':n not in failed} for n in names]},indent=2),encoding='utf-8')
    for c in initial_connections:
        try:c.close()
        except Exception:pass
    print('PHOTO_PROMPT_BACKEND_PRESERVED_SUMMARY total=%d passed=%d failed=%d'%(result.testsRun,result.testsRun-len(failed),len(failed)))
    raise SystemExit(0 if result.wasSuccessful() else 1)
