"""Output mode integration: isolated code/data and disabled external network."""
import copy
import json
import time
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace
from test_workflow import WorkflowTests, m, base, OUTPUT, initial_connections
from template_output import select_template_output, SINGLE_OUTPUT_RULE


class OutputTests(WorkflowTests):
    def template(self):
        return {'id':'poster','name':'上下拼图','engine':'fine','prompt':'上半放原图，下半水彩',
                'layout':'postcard_bottom','text_fields':[{'key':'title','default':'旧标题'}],'price':40}

    def test_output_snapshot_and_original_are_preserved(self):
        template=self.template();original=copy.deepcopy(template)
        single=select_template_output(template,'single')
        self.assertEqual(template,original)
        self.assertTrue(single['prompt'].endswith(SINGLE_OUTPUT_RULE))
        self.assertEqual(single['layout'],'');self.assertEqual(single['text_fields'],[])
        self.assertEqual(single['price'],40);self.assertEqual(single['engine'],'fine')
        self.assertEqual(select_template_output(template)['prompt'],original['prompt'])
        self.assertEqual(select_template_output(template)['text_fields'],original['text_fields'])

    def test_output_invalid_mode_is_rejected(self):
        for mode in ('crop','',None):
            with self.assertRaises(ValueError):select_template_output(self.template(),mode)
        with self.assertRaises(ValueError):select_template_output(None,'single')
        self.assertIsNone(select_template_output(None))

    def test_output_multipart_and_cos_use_same_snapshot(self):
        captured=[]
        def register(*args,**kw):
            captured.append(kw);Path(args[3]).unlink(missing_ok=True)
            return {'code':0,'job_id':'abcabc123456'}
        with patch.object(m,'_resolve_template',return_value=(self.template(),'fine',{'title':'旧标题'})), \
             patch.object(m,'_register_job',side_effect=register):
            response=self.client.post('/api/rescue',headers=self.headers,
                data={'template_id':'poster','template_output_mode':'single','aspect_ratio':'1:1'},
                files={'image':('a.jpg',self.image(),'image/jpeg')})
            self.assertEqual(response.status_code,200,response.text)
            upload_id='isolated_upload'
            with m._uploads_lock:
                m._uploads[upload_id]={'openid':'sample_user','key':'incoming/test.jpg','created_at':time.time(),'ext':'.jpg'}
            with patch.object(m,'cos_head',return_value=True),patch.object(m,'cos_get',return_value=self.image()):
                response=self.client.post('/api/rescue/by-upload',headers=self.headers,
                    json={'upload_id':upload_id,'template_id':'poster','template_output_mode':'single'})
            self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(len(captured),2)
        for kw in captured:
            self.assertEqual(kw['template']['output_mode'],'single')
            self.assertEqual(kw['template']['text_fields'],[]);self.assertEqual(kw['text_values'],{})

    def test_output_invalid_input_rejects_before_upload_read(self):
        response=self.client.post('/api/rescue/by-upload',headers=self.headers,
                                  json={'upload_id':'missing','template_output_mode':'bogus'})
        self.assertEqual(response.status_code,400,response.text)
        response=self.client.post('/api/rescue/by-upload',headers=self.headers,
                                  json={'upload_id':'missing','template_output_mode':'single'})
        self.assertEqual(response.status_code,400,response.text)

    def test_output_unavailable_prompt_engine_does_not_charge(self):
        single=select_template_output(self.template(),'single')
        with patch.object(m,'_get_client',return_value=None),patch.object(m.users,'reserve_job') as reserve:
            with self.assertRaises(m.HTTPException) as caught:
                m._register_job('sample_user','fine','clear','unused.jpg','.jpg',template=single)
        self.assertEqual(caught.exception.status_code,503);reserve.assert_not_called()

    def test_output_pipeline_preserves_style_and_skips_text_overlay(self):
        jid=self.job();m.jobs.update(jid,status='processing');sent=[]
        def enhance(src,out,**kw):
            sent.append(kw);Path(out).write_bytes(self.image((90,160)))
        m.settings.update({'providers':{'worldcodes':{'enabled':True}}})
        client=SimpleNamespace(configured=True,enhance=enhance)
        with patch.object(m,'_get_client',return_value=client),patch.object(m,'apply_text_overlay') as overlay:
            m._run_pipeline(jid,'fine','clear',select_template_output(self.template(),'single'),{'title':'旧标题'},'1:1')
        self.assertEqual(m.jobs.get(jid)['status'],'succeeded')
        self.assertEqual((m.jobs.get(jid)['width'],m.jobs.get(jid)['height']),(90,160))
        self.assertIn(SINGLE_OUTPUT_RULE,sent[0]['prompt']);overlay.assert_not_called()

    def test_output_engine_failure_does_not_silently_return_plain_photo(self):
        jid=self.job();m.jobs.update(jid,status='processing');m.settings.update({'providers':{'worldcodes':{'enabled':True}}})
        def fail(*args,**kw):raise RuntimeError('isolated provider failure')
        client=SimpleNamespace(configured=True,enhance=fail)
        with patch.object(m,'_get_client',return_value=client),patch.object(m.engine,'process') as local:
            m._run_pipeline(jid,'fine','clear',select_template_output(self.template(),'single'))
        self.assertEqual(m.jobs.get(jid)['status'],'failed');local.assert_not_called()

    def test_output_config_advertises_capability(self):
        self.assertEqual(self.client.get('/api/config').json()['template_output_modes'],['template','single'])


if __name__=='__main__':
    rows=[]
    class RecordResult(unittest.TextTestResult):
        def addSuccess(self,test):
            super().addSuccess(test);rows.append({'case':test._testMethodName,'passed':True})
        def addFailure(self,test,err):
            super().addFailure(test,err);rows.append({'case':test._testMethodName,'passed':False})
        def addError(self,test,err):
            super().addError(test,err);rows.append({'case':test._testMethodName,'passed':False,'harness_error':str(err[1])})
    suite=unittest.TestSuite(OutputTests(n) for n in OutputTests.__dict__ if n.startswith('test_output_'))
    result=unittest.TextTestRunner(verbosity=2,resultclass=RecordResult).run(suite)
    (OUTPUT/'template_output_results.json').write_text(json.dumps({'cases':rows},ensure_ascii=False,indent=2),encoding='utf-8')
    print('TEMPLATE_OUTPUT_SUMMARY total=%d passed=%d failed=%d' %
          (result.testsRun,result.testsRun-len(result.errors)-len(result.failures),len(result.errors)+len(result.failures)))
    for connection in initial_connections:
        try:connection.close()
        except Exception:pass
    raise SystemExit(0 if result.wasSuccessful() else 1)
