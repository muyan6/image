"""Full-review fixes: CI batching, complete body text and Baidu token recovery.

REVIEW_ROOT selects an immutable baseline tree. All media/data are synthetic,
all provider/COS calls are intercepted by fixtures, and no live data are read.
"""
import base64
import copy
import hashlib
import json
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw
import requests
from test_cloud_pipeline import CloudTests, m, cp, AsyncImages, OUTPUT, initial_connections
import text_overlay
import cloud_layout
from templates_store import _seed_templates, _seed_groups, _validate
from baidu_ai import BaiduImageEnhance, BaiduError, TOKEN_URL


class FullReviewMediaTests(CloudTests):
    def multiline(self):
        template=copy.deepcopy(next(t for t in _seed_templates() if t['id']=='t_postcard'))
        values=text_overlay.collect_values(template['text_fields'],{'place':'\n'.join(['地']*11)})
        cp.cos.image_info.return_value={'width':1024,'height':1536}
        jid=self.new(template=template,text_values=values)
        self.step(jid,'prepare')
        with patch.object(AsyncImages,'submit',return_value='imgtask_fixture') as submit:
            self.step(jid,'submit')
        self.assertEqual(submit.call_count,1)
        return jid

    def ci_fixture(self, *, failure=None, cancelled=False):
        seen=[];failed=[False]
        def process(settings,source,target,rule):
            layers=len(rule.split('|'));seen.append((source,target,layers,rule))
            if layers>10:raise cp.cos.CosError('Maximum ten CI layers',status=400)
            if target.endswith('.jpg') and '_layout_' not in target:
                if cancelled:m.jobs.delete_for_openid(self.current_jid,'sample_user')
                if failure and not failed[0]:
                    failed[0]=True
                    raise cp.cos.CosError('Synthetic CI rejection',status=failure)
            return {'size':1000}
        return seen,process

    def test_ci_multiline_builtin_finishes_with_each_batch_at_most_ten_layers(self):
        jid=self.multiline();seen,process=self.ci_fixture()
        with patch.object(cp.cos,'process_image',side_effect=process):job=self.completed(jid)
        self.assertEqual(job['status'],'succeeded',job.get('error'))
        self.assertEqual([entry[2] for entry in seen],[10,4])
        self.assertEqual(job['cloud_output_batches_done'],2)
        self.assertEqual(m.users.get_balance('sample_user'),60)
        self.assertEqual(len(job['cloud_layout_keys']),1)
        for key in job['cloud_layout_keys']:
            due=m.cleanup._conn.execute("SELECT due FROM cleanup WHERE kind='cos' AND target=?",(key,)).fetchone()[0]
            self.assertLessEqual(due,time.time())

    def test_ci_restart_resumes_at_unacknowledged_batch_not_paid_generation(self):
        jid=self.multiline();seen,process=self.ci_fixture(failure=503)
        with patch.object(cp.cos,'process_image',side_effect=process):
            self.completed(jid)
            job=m.jobs.get(jid);self.assertEqual(job['status'],'processing',job.get('error'))
            self.assertEqual(job['cloud_output_batches_done'],1)
            db=m.jobs._db_path;m.jobs._conn.close();m.jobs=m.JobStore(2592000,5000,db_path=db)
            with patch.object(AsyncImages,'submit',side_effect=AssertionError('PAID_REPLAY')):
                self.step(jid,'finalize')
        job=m.jobs.get(jid);self.assertEqual(job['status'],'succeeded',job.get('error'))
        self.assertEqual([entry[2] for entry in seen],[10,4,4])
        self.assertEqual(seen[0][1],seen[1][0]);self.assertEqual(seen[1][0],seen[2][0])
        self.assertEqual(m.users.get_balance('sample_user'),60)

    def test_ci_batch_failure_cleans_intermediate_objects_and_refunds(self):
        jid=self.multiline();seen,process=self.ci_fixture(failure=400)
        with patch.object(cp.cos,'process_image',side_effect=process):job=self.completed(jid)
        self.assertEqual(job['status'],'failed');self.assertEqual(m.users.get_balance('sample_user'),100)
        self.assertTrue(job['cloud_layout_keys'])
        for key in job['cloud_layout_keys']:
            self.assertLessEqual(m.cleanup._conn.execute("SELECT due FROM cleanup WHERE target=?",(key,)).fetchone()[0],time.time())

    def test_ci_deletion_during_last_batch_never_delivers_and_cleans_intermediate(self):
        jid=self.multiline();self.current_jid=jid;seen,process=self.ci_fixture(cancelled=True)
        with patch.object(cp.cos,'process_image',side_effect=process),patch.object(m.cleanup,'delete_cos_now',return_value=0):
            job=self.completed(jid)
        self.assertTrue(job.get('deleted_at'));self.assertIsNone(m._job_media_url(job,'result'))
        for key in job['cloud_layout_keys']:
            self.assertIn(key,m._job_cos_keys(job))
            self.assertLessEqual(m.cleanup._conn.execute("SELECT due FROM cleanup WHERE target=?",(key,)).fetchone()[0],time.time())

    def test_ci_single_batch_photo_still_persists_exactly_one_final_object(self):
        jid=self.generating();cp.cos.process_image.reset_mock()
        job=self.completed(jid)
        self.assertEqual(job['status'],'succeeded',job.get('error'))
        cp.cos.process_image.assert_called_once()
        self.assertEqual(cp.cos.process_image.call_args.args[2],job['result_cos'])

    def test_ci_long_body_keeps_readable_font_and_all_characters(self):
        template={'layout':'poster_center','text_fields':[{'key':'body','role':'body'}]}
        body='人间山川值得珍惜'*25
        rule=cloud_layout.text_rule(1024,1536,template,{'body':body})
        fonts=[];lines=[]
        for layer in rule.split('|'):
            parts=layer.split('/');fonts.append(int(parts[parts.index('fontsize')+1]))
            text=parts[parts.index('text')+1];lines.append(base64.urlsafe_b64decode(text+'='*((-len(text))%4)).decode('utf8'))
        # South-gravity rules are emitted bottom-up so visible reading order is reversed.
        self.assertEqual(''.join(reversed(lines)),body);self.assertGreater(len(lines),1)
        self.assertGreaterEqual(min(fonts),8)

    def test_ci_unrenderable_field_is_rejected_before_any_charge_or_paid_submit(self):
        template=copy.deepcopy(next(t for t in _seed_templates() if t['id']=='t_postcard'))
        template['text_fields']=[{'key':'dense','role':'body','label':'正文','default':'','max_len':200}]
        with patch.object(m.users,'reserve_job',wraps=m.users.reserve_job) as reserve,patch.object(AsyncImages,'submit') as submit:
            with self.assertRaises(m.HTTPException) as caught:
                self.new(template=template,text_values={'dense':'\n'.join(['文']*100)})
        self.assertEqual(caught.exception.status_code,400)
        reserve.assert_not_called();submit.assert_not_called()
        self.assertEqual(m.users.get_balance('sample_user'),100)

    def body_image(self,layout,body):
        template=copy.deepcopy(next(t for t in _seed_templates() if t['id']=='t_postcard'))
        template.update(layout=layout,text_fields=[{'key':'message','label':'正文','role':'body','default':'','max_len':200}])
        _validate({'groups':_seed_groups(),'templates':[template]})
        source=self.d/'source.jpg';empty=self.d/(layout+'-empty.jpg');out=self.d/(layout+'-body.jpg')
        Image.new('RGB',(1024,1536),'#456789').save(source,quality=95)
        text_overlay.apply(str(source),str(empty),layout,template['text_fields'],{})
        calls=[];original=ImageDraw.ImageDraw.text
        def record(draw,xy,text,*args,**kwargs):
            if text:calls.append((xy,text,kwargs.get('font')))
            return original(draw,xy,text,*args,**kwargs)
        with patch.object(ImageDraw.ImageDraw,'text',new=record):
            text_overlay.apply(str(source),str(out),layout,template['text_fields'],{'message':body})
        with Image.open(out) as rendered:
            rendered.save(OUTPUT/(layout+'-body-preview.png'))
        self.assertNotEqual(hashlib.sha256(out.read_bytes()).hexdigest(),hashlib.sha256(empty.read_bytes()).hexdigest())
        self.assertEqual(''.join(text for _,text,_ in calls),body.replace('\n',''))
        for (x,y),text,font in calls:
            self.assertGreaterEqual(x,0);self.assertGreaterEqual(y,0)
            self.assertLessEqual(y+font.size,1536)
            self.assertLessEqual(x+font.getlength(text),1024)

    def test_local_body_poster_wraps_complete_long_text(self):
        self.body_image('poster_center','正文第一段\n'+'山川有光人间有爱'*24)

    def test_local_body_postcard_renders_all_paragraphs(self):
        self.body_image('postcard_bottom','明信片正文\n'+'沿途风景值得记录'*24)

    def test_local_body_stamp_renders_even_without_date_or_place(self):
        self.body_image('stamp_corner','邮戳正文\n'+'留住每一段珍贵记忆'*23)

    def baidu_session(self,error,following):
        calls=[]
        def response(status,data):
            r=Mock(status_code=status);r.json.return_value=data;r.text=json.dumps(data);return r
        def post(url,**kwargs):
            if url==TOKEN_URL:
                calls.append(('token',None));return response(200,{'access_token':'fresh-token','expires_in':3600})
            token=kwargs['params']['access_token'];calls.append(('image',token))
            return response(*error) if token=='cached-token' else response(*following)
        session=Mock();session.post.side_effect=post
        client=BaiduImageEnhance('fixture','fixture',session=session)
        client._token='cached-token';client._token_expires_at=time.time()+1800
        source=self.d/'baidu-input.jpg';source.write_bytes(self.image())
        return client,source,self.d/'baidu-output.jpg',calls

    def test_baidu_business_token_110_and_111_refresh_once_then_finish(self):
        image=base64.b64encode(self.image()).decode('ascii')
        for code in (110,111,'110','111'):
            client,source,out,calls=self.baidu_session((200,{'error_code':code,'error_msg':'invalid token'}),(200,{'image':image}))
            client.enhance(str(source),str(out),quality='light')
            self.assertEqual(calls,[('image','cached-token'),('token',None),('image','fresh-token')])
            self.assertEqual(out.read_bytes(),self.image())

    def test_baidu_repeat_token_rejection_is_bounded_and_invalidates_cache(self):
        for first,second in [((401,{}),(401,{})),((200,{'error_code':110}),(200,{'error_code':111}))]:
            client,source,out,calls=self.baidu_session(first,second)
            with self.assertRaises(BaiduError) as caught:client.enhance(str(source),str(out),quality='light')
            self.assertEqual(caught.exception.code,'AUTH')
            self.assertEqual(calls,[('image','cached-token'),('token',None),('image','fresh-token')])
            self.assertEqual(client._token_expires_at,0)

    def test_baidu_ambiguous_error_after_refresh_is_not_paid_replayed(self):
        client,source,out,calls=self.baidu_session((200,{'error_code':111}),(503,{}))
        with self.assertRaises(BaiduError) as caught:client.enhance(str(source),str(out),quality='light')
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(calls,[('image','cached-token'),('token',None),('image','fresh-token')])

    def test_baidu_network_timeout_is_not_token_refreshed_or_replayed(self):
        client,source,out,calls=self.baidu_session((200,{}),(200,{}))
        client.session.post.side_effect=requests.Timeout()
        with self.assertRaises(BaiduError) as caught:client.enhance(str(source),str(out),quality='light')
        self.assertTrue(caught.exception.uncertain);self.assertEqual(client.session.post.call_count,1)


if __name__=='__main__':
    names=[name for name in FullReviewMediaTests.__dict__ if name.startswith('test_')]
    suite=unittest.TestSuite(FullReviewMediaTests(name) for name in names)
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    failed={test._testMethodName for test,_ in result.failures+result.errors}
    (OUTPUT/'full_review_media_results.json').write_text(json.dumps({'cases':[{'case':name,'passed':name not in failed} for name in names]},indent=2),encoding='utf8')
    for connection in initial_connections:
        try:connection.close()
        except Exception:pass
    print('FULL_REVIEW_MEDIA_SUMMARY total=%d passed=%d failed=%d'%(result.testsRun,result.testsRun-len(failed),len(failed)))
    raise SystemExit(0 if result.wasSuccessful() else 1)
