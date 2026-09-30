"""CI, retention and concurrency checks: isolated data; no paid cloud calls."""
import hashlib,hmac,json,threading,time,unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from test_workflow import WorkflowTests,m,OUTPUT,initial_connections
import cos_store
from image_processing import BoundedExecutor,QueueFull,image_limited,normalization_rule

class CITests(WorkflowTests):
    def test_ci_rule_uses_basic_operations_and_center_crop(self):
        rule=normalization_rule(1600,900,1536,'1:1')
        self.assertIn('crop/900x900',rule);self.assertIn('thumbnail/1536x1536>',rule)
        self.assertIn('quality/85',rule);self.assertNotIn('guetzli',rule)
        self.assertNotIn('crop',normalization_rule(1600,900,1536,''))
        with self.assertRaises(ValueError):normalization_rule(100,100,1536,'0:1')

    def test_ci_signature_covers_operations_header_and_query(self):
        conf={'secret_id':'fixture-id','secret_key':'fixture-secret','cos_bucket':'fixture-123','cos_region':'ap-guangzhou','cos_custom_domain':''}
        with patch.object(m.settings,'tencent',return_value=conf),patch.object(cos_store.time,'time',return_value=1000):
            a=cos_store.presign(m.settings,'post','source.jpg',params={'image_process':''},headers={'Pic-Operations':'{"rules":[]}'})
            b=cos_store.presign(m.settings,'post','source.jpg',params={'image_process':''},headers={'Pic-Operations':'{"rules":[1]}'})
        self.assertNotEqual(a.split('q-signature=')[1],b.split('q-signature=')[1])
        self.assertIn('q-header-list=pic-operations',a);self.assertIn('q-url-param-list=image_process',a)
        self.assertIn('image_process=',a)

    def test_ci_cloud_request_has_no_image_bytes_and_keeps_source(self):
        with patch.object(m.settings,'tencent',return_value={'secret_id':'fixture','secret_key':'fixture','cos_bucket':'fixture-123','cos_region':'ap-guangzhou'}), \
             patch.object(cos_store,'control_request',return_value=SimpleNamespace(status_code=200)) as post, \
             patch.object(cos_store,'object_metadata',return_value={'size':1234}):
            cos_store.process_image(m.settings,'orig.jpg','comparisons/new.jpg','imageMogr2/thumbnail/1536x1536>')
        self.assertEqual(post.call_args.kwargs['data'],b'')
        self.assertEqual(json.loads(post.call_args.kwargs['headers']['Pic-Operations'])['rules'][0]['fileid'],'/comparisons/new.jpg')
        with self.assertRaises(ValueError):cos_store.process_image(m.settings,'same.jpg','same.jpg','bad')

    def test_ci_pipeline_uses_cloud_norm_and_keeps_comparison_after_24h(self):
        jid=self.job(original_age=90000);m.jobs.update(jid,status='processing',orig_cos='origins/raw.jpg')
        m.settings.update({'processing':{'ci_enabled':True},'providers':{'worldcodes':{'enabled':True}}})
        def enhance(src,out,**kw):Path(out).write_bytes(self.image((90,90)))
        client=SimpleNamespace(configured=True,enhance=enhance)
        with patch.object(m.settings,'cos_ready',return_value=True),patch.object(m,'cos_process_image') as process, \
             patch.object(m,'cos_get',return_value=self.image((90,90))),patch.object(m,'cos_put'), \
             patch.object(m,'cos_head',return_value=True),patch.object(m,'cos_presign',side_effect=lambda s,method,key,**kw:'https://cos.invalid/'+key), \
             patch.object(m,'_get_client',return_value=client),patch.object(m,'_normalize_long_side') as local:
            m._run_pipeline(jid,'light','clear',aspect_ratio='1:1')
            job=m.jobs.get(jid);self.assertEqual(job['status'],'succeeded',job.get('error'))
            self.assertEqual(job['processing_mode'],'ci');local.assert_not_called();process.assert_called_once()
            self.assertEqual(job['orig_cos'],'origins/raw.jpg')
            self.assertIn('comparisons/',m._job_media_url(job,'orig'))
            self.assertGreater(m._media_expires_at(job,'orig'),time.time()+29*86400)
        self.assertEqual(set(job['timings']),{'queue_ms','normalize_ms','provider_ms','finalize_ms','storage_ms','processing_ms'})
        m._cleanup_job_files([job]);targets=[x[0] for x in m.cleanup._conn.execute('SELECT target FROM cleanup WHERE kind="cos"').fetchall()]
        self.assertIn(job['comparison_cos'],targets)

    def test_ci_failure_fails_task_without_local_full_resolution_fallback(self):
        jid=self.job();m.jobs.update(jid,status='processing',orig_cos='origins/raw.jpg')
        m.settings.update({'processing':{'ci_enabled':True}})
        with patch.object(m.settings,'cos_ready',return_value=True),patch.object(m,'cos_process_image',side_effect=m.CosError('CI disabled')), \
             patch.object(m,'_normalize_long_side') as local:
            m._run_pipeline(jid,'light','clear')
        self.assertEqual(m.jobs.get(jid)['status'],'failed');local.assert_not_called()

    def test_ci_local_work_serialized_with_16_network_threads(self):
        lock=threading.Lock();state={'active':0,'peak':0}
        @image_limited
        def work():
            with lock:state['active']+=1;state['peak']=max(state['peak'],state['active'])
            time.sleep(.005)
            with lock:state['active']-=1
        with ThreadPoolExecutor(max_workers=16) as pool:list(pool.map(lambda _:work(),range(16)))
        self.assertEqual(state['peak'],1)

    def test_ci_bounded_queue_and_exception_releases_slots(self):
        gate=threading.Event();pool=BoundedExecutor(2,1)
        try:
            futures=[pool.submit(lambda:gate.wait(3)) for _ in range(3)]
            with self.assertRaises(QueueFull):pool.submit(lambda:None)
            self.assertEqual(pool.snapshot()['capacity'],3)
            gate.set()
            for f in futures:f.result(timeout=3)
            def fail():raise RuntimeError('fixture')
            with self.assertRaises(RuntimeError):pool.submit(fail).result(timeout=3)
            self.assertEqual(pool.submit(lambda:'ok').result(timeout=3),'ok')
        finally:gate.set();pool.shutdown()

    def test_ci_old_jobs_keep_original_24h_policy(self):
        jid=self.job(original_age=90000)
        self.assertIsNone(m._job_media_url(m.jobs.get(jid),'orig'))

if __name__=='__main__':
    suite=unittest.TestSuite(CITests(n) for n in CITests.__dict__ if n.startswith('test_ci_'))
    names=[t._testMethodName for t in suite];result=unittest.TextTestRunner(verbosity=2).run(suite)
    failed={t._testMethodName for t,_ in result.failures+result.errors}
    (OUTPUT/'ci_performance_results.json').write_text(json.dumps({'cases':[{'case':n,'passed':n not in failed} for n in names]},indent=2),encoding='utf-8')
    for c in initial_connections:
        try:c.close()
        except Exception:pass
    print('CI_PERFORMANCE_SUMMARY total=%d passed=%d failed=%d'%(result.testsRun,result.testsRun-len(failed),len(failed)))
    raise SystemExit(0 if result.wasSuccessful() else 1)
