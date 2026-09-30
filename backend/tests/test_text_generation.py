"""Text mode admission/billing/COS tests, isolated and network-disabled."""
import json,unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from test_workflow import WorkflowTests,m,OUTPUT,initial_connections
from text_generation import run_text_job
from gateway_ai import OpenAIImagesEnhance
from image_processing import QueueFull

class TextTests(WorkflowTests):
    def enable(self,price=40):
        m.settings.update({'text_generation':{'enabled':True,'model':'fixture-image','price':price},
                           'providers':{'worldcodes':{'enabled':True,'base_url':'https://fixture.invalid','api_key':'fixture'}}})
    def post(self,**kw):return self.client.post('/api/text-generation',headers=self.headers,json={'prompt':'水彩森林','aspect_ratio':'1:1',**kw})

    def test_text_disabled_and_invalid_do_not_charge(self):
        self.assertEqual(self.post().status_code,503)
        self.assertEqual(self.post(prompt=' ').status_code,400)
        self.assertEqual(self.post(aspect_ratio='4:3').status_code,400)
        self.assertEqual(m.users.get_balance('sample_user'),100)
        self.assertEqual(self.client.post('/api/text-generation',json={'prompt':'a'}).status_code,401)

    def test_text_accepted_has_no_original_and_price_is_server_owned(self):
        self.enable(75)
        with patch.object(m.settings,'cos_ready',return_value=True),patch.object(m.pool,'submit'):
            r=self.post(price=0);self.assertEqual(r.status_code,200,r.text)
            jid=r.json()['job_id'];self.assertEqual(m.users.get_balance('sample_user'),25)
            self.assertEqual(m.jobs.get(jid)['orig_file'],'');self.assertEqual(m.jobs.get(jid)['input_mode'],'text')
            self.assertEqual(self.post().status_code,402)
        job=m.jobs.get(jid);self.assertEqual(job['price'],75)

    def test_text_queue_full_refunds_without_generating(self):
        self.enable()
        with patch.object(m.settings,'cos_ready',return_value=True),patch.object(m.pool,'submit',side_effect=QueueFull('full')):
            self.assertEqual(self.post().status_code,429)
        self.assertEqual(m.users.get_balance('sample_user'),100)

    def test_text_success_delivery_and_repeat_worker_do_not_charge_twice(self):
        self.enable()
        with patch.object(m.settings,'cos_ready',return_value=True),patch.object(m.pool,'submit'):
            r=self.post();jid=r.json()['job_id']
        def generate(client,path,*args):Path(path).write_bytes(self.image((90,90)))
        with patch.object(OpenAIImagesEnhance,'generate',new=generate),patch.object(m,'cos_put') as put,patch.object(m,'cos_head',return_value=True):
            conf=m.settings.snapshot()['text_generation'];provider=m.settings.provider('worldcodes')
            run_text_job(m,jid,conf,provider,'画面','1024x1024');run_text_job(m,jid,conf,provider,'画面','1024x1024')
            self.assertEqual(m.jobs.get(jid)['status'],'succeeded',m.jobs.get(jid).get('error'))
            put.assert_called_once()
        self.assertEqual(m.jobs.get(jid)['status'],'succeeded');self.assertEqual(m.users.get_balance('sample_user'),60)
        query=self.client.get('/api/jobs/'+jid,headers=self.headers).json()
        self.assertEqual(query['input_mode'],'text');self.assertFalse(query['orig_url'])

    def test_text_model_failure_refunds_and_does_not_return_blank_image(self):
        self.enable()
        with patch.object(m.settings,'cos_ready',return_value=True),patch.object(m.pool,'submit'):
            r=self.post();jid=r.json()['job_id']
        with patch.object(OpenAIImagesEnhance,'generate',side_effect=RuntimeError('model rejected')):
            run_text_job(m,jid,m.settings.snapshot()['text_generation'],m.settings.provider('worldcodes'),'画面','1024x1024')
        self.assertEqual(m.jobs.get(jid)['status'],'failed');self.assertEqual(m.users.get_balance('sample_user'),100)

    def test_text_provider_body_has_no_input_photo_or_secret_in_data(self):
        session=SimpleNamespace(post=lambda *a,**kw:None)
        client=OpenAIImagesEnhance({'base_url':'https://fixture.invalid','api_key':'secret'},session)
        with patch.object(session,'post',return_value=SimpleNamespace(status_code=200)) as post, \
             patch.object(client,'_raise_for_status'),patch.object(client,'_extract_image',return_value=self.image()),patch.object(client,'_write_output'):
            client.generate('result.jpg','森林','model','1024x1024')
        data=post.call_args.kwargs['json'];self.assertEqual(data,{'model':'model','prompt':'森林','size':'1024x1024','n':1})
        self.assertNotIn('files',post.call_args.kwargs)

if __name__=='__main__':
    suite=unittest.TestSuite(TextTests(n) for n in TextTests.__dict__ if n.startswith('test_text_'))
    names=[t._testMethodName for t in suite];result=unittest.TextTestRunner(verbosity=2).run(suite)
    failed={t._testMethodName for t,_ in result.failures+result.errors}
    (OUTPUT/'text_generation_results.json').write_text(json.dumps({'cases':[{'case':n,'passed':n not in failed} for n in names]},indent=2),encoding='utf-8')
    for c in initial_connections:
        try:c.close()
        except Exception:pass
    print('TEXT_GENERATION_SUMMARY total=%d passed=%d failed=%d'%(result.testsRun,result.testsRun-len(failed),len(failed)))
    raise SystemExit(0 if result.wasSuccessful() else 1)
