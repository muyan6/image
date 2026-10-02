"""Offline owner/admin sharing lifecycle, immutable COS grants and crash recovery."""
import copy
import hashlib
import io
import json
import sqlite3
import time
import unittest
import threading
from types import SimpleNamespace
from urllib.parse import urlsplit
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_cloud_pipeline import CloudTests,m,OUTPUT,initial_connections
from templates_store import TemplateStore
from template_shares import store_for,content_fields
import template_share_api as api


class TemplateShareTests(CloudTests):
    def setUp(self):
        super().setUp();self.extra=[];self.media={};self.copies=[];self.read_bytes=0
        self.old_templates=m.templates
        with patch.object(TemplateStore,'ensure_placeholder_covers'):m.templates=TemplateStore(str(self.d/'catalog'))
        api.reconcile(m)
        self.client.close();app=FastAPI();app.include_router(api.make_template_share_router(lambda:m))
        @app.get('/api/templates')
        def public():return {'items':m.templates.public_templates(m.settings)}
        self.client=TestClient(app);self.client.cookies.update(self.admin.cookies)
        self.admin_headers={'X-Admin-Request':'1'};self.store=store_for(m.users)
        m.users.set_nickname('sample_user','模板作者')
        def meta(settings,key):
            if key not in self.media:raise api.cos.CosError('fixture missing',status=404)
            row=self.media[key];return {'size':len(row['body']),'content_type':row['content_type']}
        def copy_object(settings,source,target,**kwargs):
            meta(settings,source);self.media[target]=copy.deepcopy(self.media[source]);self.copies.append((source,target,kwargs))
        def info(settings,key):
            meta(settings,key);return {'width':self.media[key]['width'],'height':self.media[key]['height']}
        outer=self
        class Response:
            status_code=206;headers={}
            def __init__(self,key):self.key=key
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def iter_content(self,size):
                for offset in range(0,len(outer.media[self.key]['body']),size):
                    chunk=outer.media[self.key]['body'][offset:offset+size];outer.read_bytes+=len(chunk);yield chunk
        def control(settings,method,key,**kwargs):
            self.assertEqual(method,'GET');self.assertEqual(kwargs['headers'],{'Range':'bytes=0-15'});self.assertTrue(kwargs['stream']);return Response(key)
        for name,fn in (('object_metadata',meta),('copy_object',copy_object),('image_info',info),('control_request',control)):
            item=patch.object(api.cos,name,side_effect=fn);item.start();self.extra.append(item)
    def tearDown(self):
        for item in reversed(self.extra):item.stop()
        m.templates=self.old_templates
        super().tearDown()
    def create(self,**kwargs):
        data={'request_id':'request_'+str(time.time_ns()),'name':'共享胶片','subtitle':'自然色彩','prompt':'保持主体，微暖胶片','guide':{'advice':'选清晰人像'},**kwargs}
        response=self.client.post('/api/template-shares',headers=self.headers,json=data)
        self.assertEqual(response.status_code,200,response.text);return response.json()['submission']
    def upload(self,row,*,ext='jpg',body=None,width=480,height=640):
        image=body if body is not None else self.image((width,height))
        content_type={'jpg':'image/jpeg','png':'image/png','webp':'image/webp'}[ext]
        response=self.client.post('/api/template-shares/uploads',headers=self.headers,json={'share_id':row['id'],'revision':row['revision'],'ext':ext,'size':len(image),'content_type':content_type})
        self.assertEqual(response.status_code,200,response.text);data=response.json()
        key=urlsplit(data['url']).path.lstrip('/');self.media[key]={'body':image,'content_type':content_type,'width':width,'height':height}
        result=self.client.post('/api/template-shares/uploads/'+data['upload_token']+'/complete',headers=self.headers,json={'share_id':row['id'],'revision':row['revision']})
        return result,data,key
    def ready_share(self,**kwargs):
        row=self.create(**kwargs);response,_,_=self.upload(row);self.assertEqual(response.status_code,200,response.text);return response.json()['submission']
    def submit(self,row,**kwargs):
        response=self.client.put('/api/template-shares/'+row['id'],headers=self.headers,json={'revision':row['revision'],'submit':True,'consent':True,**kwargs})
        self.assertEqual(response.status_code,200,response.text);return response.json()['submission']
    def review(self,row,**kwargs):
        return self.client.post('/admin/api/template-shares/'+row['id']+'/review',headers=self.admin_headers,json={'revision':row['revision'],'action':'approve',**kwargs})
    def published(self):return [t for t in self.client.get('/api/templates').json()['items'] if t['source']=='user']

    def test_share_draft_and_pending_are_private_without_runtime_or_balance_side_effects(self):
        row=self.ready_share();self.assertEqual(row['status'],'draft');self.assertFalse(row['has_published'])
        pending=self.submit(row);self.assertEqual(pending['status'],'pending');self.assertEqual(self.published(),[])
        self.assertEqual(pending['consent_version'],'template-sharing-v1');self.assertEqual(m.users.get_balance('sample_user'),100)
        self.assertIsNone(m.templates.get_template(pending['published_template_id'],enabled_only=True))

    def test_share_owner_auth_and_admin_cookie_header_guard(self):
        row=self.ready_share();other={'Authorization':'Bearer '+m.user_token('other_user')}
        for path in ('/api/template-shares/'+row['id'],'/api/template-shares/mine'):
            response=self.client.get(path,headers=other)
            self.assertEqual(response.status_code,404 if path.endswith(row['id']) else 200)
            if path.endswith('mine'):self.assertEqual(response.json()['items'],[])
        self.assertEqual(self.client.put('/api/template-shares/'+row['id'],headers=other,json={'revision':row['revision'],'name':'伪造'}).status_code,404)
        with TestClient(self.client.app) as anonymous:
            self.assertEqual(anonymous.get('/api/template-shares/mine').status_code,401)
            self.assertEqual(anonymous.get('/admin/api/template-shares',headers=self.admin_headers).status_code,401)
        self.assertEqual(self.client.get('/admin/api/template-shares').status_code,403)

    def test_share_unknown_extra_and_runtime_author_fields_rejected(self):
        for key,value in {'engine':'fine','price':1,'output_size':8192,'model_override':'evil','source':'official','author_openid':'other_user','group_id':'anime','covers':['https://evil.invalid/a.jpg']}.items():
            response=self.client.post('/api/template-shares',headers=self.headers,json={'request_id':'fixture_extra','name':'名称','prompt':'提示词',key:value})
            self.assertEqual(response.status_code,422,response.text)
        row=self.ready_share()
        self.assertEqual(self.review(self.submit(row),patch={'engine':'fine'}).status_code,422)

    def test_share_consent_strict_boolean_required_only_for_submit(self):
        row=self.ready_share()
        for value in (False,1,'true'):
            response=self.client.put('/api/template-shares/'+row['id'],headers=self.headers,json={'revision':row['revision'],'submit':True,'consent':value})
            self.assertIn(response.status_code,(400,422));self.assertEqual(self.store.get(row['id'])['status'],'draft')
        self.assertEqual(self.submit(row)['status'],'pending')

    def test_share_three_covers_limit_and_same_submission_tokens_only(self):
        row=self.create()
        for _ in range(3):
            response,_,_=self.upload(row);self.assertEqual(response.status_code,200,response.text);row=response.json()['submission']
        response,_,_=self.upload(row);self.assertEqual(response.status_code,409)
        other=self.ready_share()
        response=self.client.put('/api/template-shares/'+row['id'],headers=self.headers,json={'revision':row['revision'],'cover_tokens':[other['cover_tokens'][0]],'submit':True,'consent':True})
        self.assertEqual(response.status_code,409);self.assertEqual(self.store.get(row['id'])['payload']['cover_tokens'],row['cover_tokens'])

    def test_share_upload_magic_size_dimensions_and_grant_boundaries(self):
        for image,width,height in ((b'not an image',480,640),(self.image((50,50)),50,50),(self.image(),2000,100)):
            row=self.create();response,_,_=self.upload(row,body=image,width=width,height=height);self.assertEqual(response.status_code,409,response.text)
        row=self.create();response,data,key=self.upload(row);self.assertEqual(response.status_code,200)
        self.assertEqual(self.read_bytes,60)
        self.assertEqual(data['headers']['x-cos-acl'],'private');self.assertIn('q-header-list=content-type%3Bx-cos-acl',data['url'])
        foreign=self.create()
        r=self.client.post('/api/template-shares/uploads/'+data['upload_token']+'/complete',headers=self.headers,json={'share_id':foreign['id'],'revision':foreign['revision']})
        self.assertEqual(r.status_code,404)

    def test_share_upload_copy_freezes_final_object_before_magic_check(self):
        row=self.create();response,data,key=self.upload(row);self.assertEqual(response.status_code,200)
        final=self.copies[-1][1];before=hashlib.sha256(self.media[final]['body']).hexdigest()
        self.media[key]['body']=b'overwritten temporary source'
        repeated=self.client.post('/api/template-shares/uploads/'+data['upload_token']+'/complete',headers=self.headers,json={'share_id':row['id'],'revision':row['revision']})
        self.assertEqual(repeated.status_code,200);self.assertEqual(len(self.copies),1)
        self.assertEqual(hashlib.sha256(self.media[final]['body']).hexdigest(),before)
        self.assertNotIn(final,data['url']);self.assertTrue(self.copies[-1][2]['private'])

    def test_share_stable_identity_name_duplicates_allowed_and_original_catalog_untouched(self):
        official=copy.deepcopy(m.templates.list_templates())
        a=self.submit(self.ready_share());b=self.submit(self.ready_share())
        self.assertEqual(self.review(a).status_code,200);self.assertEqual(self.review(b).status_code,200)
        shared=self.published();self.assertEqual(len(shared),2);self.assertEqual(shared[0]['name'],shared[1]['name'])
        self.assertNotEqual(a['published_template_id'],b['published_template_id'])
        self.assertEqual([t for t in m.templates.list_templates() if t.get('source')!='user'],official)
        raw=m.templates.get_template(a['published_template_id']);self.assertEqual(raw['author_openid'],'sample_user');self.assertGreater(raw['source_revision'],0)
        self.assertNotIn('author_openid',shared[0]);self.assertNotIn('prompt',shared[0]);self.assertEqual(shared[0]['group_id'],'')

    def test_share_edit_approved_keeps_old_until_review_and_first_publication_timestamp(self):
        first=self.submit(self.ready_share());response=self.review(first);self.assertEqual(response.status_code,200)
        approved=response.json()['submission'];old=self.published()[0];stamp=old['published_at']
        edited=self.client.put('/api/template-shares/'+first['id'],headers=self.headers,json={'revision':approved['revision'],'name':'第二版','prompt':'水彩','submit':True,'consent':True})
        self.assertEqual(edited.status_code,200,edited.text);pending=edited.json()['submission']
        self.assertTrue(pending['has_published']);self.assertEqual(self.published()[0]['name'],old['name'])
        self.assertEqual(self.review(pending).status_code,200);new=self.published()[0]
        self.assertEqual(new['name'],'第二版');self.assertEqual(new['id'],old['id']);self.assertEqual(new['published_at'],stamp)

    def test_share_reject_new_version_preserves_approved_and_stale_revision_conflicts(self):
        row=self.submit(self.ready_share());approved=self.review(row).json()['submission']
        edit=self.client.put('/api/template-shares/'+row['id'],headers=self.headers,json={'revision':approved['revision'],'name':'待审新名','submit':True,'consent':True}).json()['submission']
        rejected=self.review(edit,action='reject',reason='调整指南');self.assertEqual(rejected.status_code,200)
        self.assertEqual(rejected.json()['submission']['status'],'rejected');self.assertEqual(self.published()[0]['name'],'共享胶片')
        self.assertEqual(self.review(approved).status_code,409)

    def test_share_admin_text_guide_and_existing_group_patch(self):
        row=self.submit(self.ready_share());response=self.review(row,patch={'name':'审核后名称','guide':{'advice':'选择半身','tips':['避免遮挡']},'group_id':'anime'})
        self.assertEqual(response.status_code,200,response.text);public=self.published()[0]
        self.assertEqual(public['name'],'审核后名称');self.assertEqual(public['guide']['tips'],['避免遮挡']);self.assertEqual(public['group_id'],'anime')
        self.assertEqual(self.review(response.json()['submission'],patch={'group_id':'brand_new'}).status_code,400)

    def test_share_publication_file_failure_recovers_idempotently(self):
        row=self.submit(self.ready_share())
        with patch.object(m.templates,'_save_locked',side_effect=OSError('fixture disk full')):failed=self.review(row)
        self.assertEqual(failed.status_code,503);self.assertEqual(self.published(),[])
        saved=self.store.get(row['id']);self.assertEqual(saved['catalog_dirty'],1)
        self.assertEqual(api.reconcile(m)['failed'],0);self.assertEqual(len(self.published()),1)
        first=m.templates.get_template(row['published_template_id'])
        with patch.object(m.templates,'_save_locked',wraps=m.templates._save_locked) as writing:
            api.reconcile(m);self.assertEqual(writing.call_count,0)
        self.assertEqual(m.templates.get_template(row['published_template_id']),first)

    def test_share_publish_after_catalog_commit_before_sql_mark_recovers(self):
        row=self.submit(self.ready_share())
        with patch.object(self.store,'mark_applied',side_effect=OSError('fixture crash')):failed=self.review(row)
        self.assertEqual(failed.status_code,503);self.assertEqual(self.published(),[])
        self.assertEqual(api.reconcile(m)['failed'],0);self.assertEqual(len(self.published()),1)
        self.assertEqual(self.store.get(row['id'])['status'],'approved')

    def test_share_withdraw_projection_failure_hides_immediately_and_recovers(self):
        row=self.submit(self.ready_share());approved=self.review(row).json()['submission'];self.assertEqual(len(self.published()),1)
        with patch.object(m.templates,'_save_locked',side_effect=OSError('fixture disk full')):
            r=self.client.post('/api/template-shares/'+row['id']+'/withdraw',headers=self.headers,json={'revision':approved['revision']})
        self.assertEqual(r.status_code,503);self.assertEqual(self.published(),[])
        self.assertIsNone(m.templates.get_template(row['published_template_id'],enabled_only=True))
        self.assertEqual(api.reconcile(m)['failed'],0);self.assertFalse(m.templates.get_template(row['published_template_id'])['enabled'])

    def test_share_alias_owner_and_ban_visibility_source_invalidation(self):
        row=self.submit(self.ready_share());self.assertEqual(self.review(row).status_code,200);self.assertEqual(len(self.published()),1)
        m.users.set_banned('sample_user',True);self.assertEqual(self.published(),[])
        self.assertIsNone(m.templates.get_template(row['published_template_id'],enabled_only=True))
        self.assertEqual(self.client.post('/api/template-shares',headers=self.headers,json={'request_id':'banned_share'}).status_code,403)
        m.users.set_banned('sample_user',False);self.assertEqual(len(self.published()),1)
        with m.users._lock,m.users._conn:
            m.users._conn.execute('INSERT INTO account_aliases VALUES(?,?,?,?,?)',('alias_sample','sample_user',time.time(),'fixture_union','fixture_app'))
        alias={'Authorization':'Bearer '+m.user_token('alias_sample')}
        self.assertEqual(self.client.get('/api/template-shares/'+row['id'],headers=alias).status_code,200)
        self.assertEqual(len(self.client.get('/api/template-shares/mine',headers=alias).json()['items']),1)

    def test_share_external_connection_unpublish_invalidates_cached_projection(self):
        row=self.submit(self.ready_share());self.assertEqual(self.review(row).status_code,200);self.assertEqual(len(self.published()),1)
        db=sqlite3.connect(m.users._path)
        try:
            with db:db.execute('UPDATE template_shares SET catalog_enabled=0 WHERE id=?',(row['id'],))
            self.assertEqual(self.published(),[])
        finally:db.close()

    def test_share_cleanup_precise_prefix_and_approved_retention(self):
        row=self.ready_share();cover=self.store.covers(self.store.get(row['id']))[0];key=cover['final_key']
        self.assertIsNotNone(m.cleanup._conn.execute('SELECT due FROM cleanup WHERE target=?',(key,)).fetchone())
        approved=self.submit(row);self.assertEqual(self.review(approved).status_code,200)
        self.assertIsNone(m.cleanup._conn.execute('SELECT due FROM cleanup WHERE target=?',(key,)).fetchone())
        self.assertIsNone(m.cleanup._conn.execute('SELECT target FROM upload_cleanup_guard WHERE target=?',(key,)).fetchone())
        for bad in ('template-shares/private.jpg','template-shares/'+row['id']+'/uploads/fake.jpg','template-shares/'+row['id']+'/covers/../private.jpg','results/private.jpg'):
            with self.assertRaises(ValueError):m.cleanup.protect_copy_until(bad,time.time()+600)
            with self.assertRaises(ValueError):m.cleanup.cancel('cos',bad)

    def test_share_cleanup_source_expiry_does_not_delete_final_approved_copy(self):
        row=self.ready_share();pending=self.submit(row);self.assertEqual(self.review(pending).status_code,200)
        source,final,_=self.copies[0]
        with patch('cleanup_store.delete_object',side_effect=lambda settings,key,**kw:self.media.pop(key,None)):
            m.cleanup.delete_cos_now(m.settings,[source])
        self.assertNotIn(source,self.media);self.assertIn(final,self.media);self.assertEqual(len(self.published()),1)

    def test_share_request_id_idempotence_and_reward_summary_owner_admin(self):
        first=self.create(request_id='same_request_id');second=self.create(request_id='same_request_id')
        self.assertEqual(first['id'],second['id'])
        mine=self.client.get('/api/template-shares/mine',headers=self.headers)
        self.assertEqual(mine.status_code,200,mine.text);self.assertIn('reward_earned',mine.json()['rewards'])
        detail=self.client.get('/admin/api/template-shares/'+first['id'],headers=self.admin_headers)
        self.assertEqual(detail.status_code,200);self.assertIn('reward_credited',detail.json()['submission']['reward_summary'])

    def test_share_feature_switch_hides_cached_and_generation_lookup_immediately(self):
        row=self.submit(self.ready_share());self.assertEqual(self.review(row).status_code,200);self.assertEqual(len(self.published()),1)
        m.settings.update({'template_sharing':{'enabled':False}})
        self.assertEqual(self.published(),[]);self.assertIsNone(m.templates.get_template(row['template_id'],enabled_only=True))
        self.assertEqual(self.client.post('/api/template-shares',headers=self.headers,json={'request_id':'feature_disabled'}).status_code,503)
        m.settings.update({'template_sharing':{'enabled':True}});self.assertEqual(len(self.published()),1)

    def test_share_upload_wrong_declared_size_and_foreign_owner_do_not_confirm(self):
        row=self.create();image=self.image();issued=self.client.post('/api/template-shares/uploads',headers=self.headers,json={'share_id':row['id'],'revision':row['revision'],'ext':'jpg','size':len(image)+1,'content_type':'image/jpeg'}).json()
        key=urlsplit(issued['url']).path.lstrip('/');self.media[key]={'body':image,'content_type':'image/jpeg','width':480,'height':640}
        path='/api/template-shares/uploads/'+issued['upload_token']+'/complete';body={'share_id':row['id'],'revision':row['revision']}
        response=self.client.post(path,headers=self.headers,json=body);self.assertEqual(response.status_code,409);self.assertEqual(self.copies,[])
        other={'Authorization':'Bearer '+m.user_token('other_user')}
        self.assertEqual(self.client.post(path,headers=other,json=body).status_code,404);self.assertEqual(self.copies,[])

    def test_share_guides_reject_instead_of_silently_truncating(self):
        response=self.client.post('/api/template-shares',headers=self.headers,json={'request_id':'guide_long_fixture','guide':{'tips':['字'*61]}})
        self.assertEqual(response.status_code,422)
        with self.assertRaises(ValueError):content_fields({'guide':{'tips':['字'*61]}})

    def test_share_more_than_200_visibility_and_recovery_are_not_truncated(self):
        now=time.time();rows=[];ids=[]
        for n in range(201):
            sid='ts_'+'%032x'%n;tid='u_'+'%030x'%n;ids.append(tid)
            payload={'name':'名称','subtitle':'','prompt':'提示词','group_id':'','guide':{},'cover_tokens':['%032x'%n]}
            approved={**payload,'covers':['cos:template-shares/'+sid+'/covers/'+'%032x'%n+'.jpg']}
            rows.append((sid,'sample_user','bulk_%08d'%n,tid,json.dumps(payload),json.dumps(approved),now,now))
        with m.users._lock,m.users._conn:
            m.users._conn.executemany("INSERT INTO template_shares(id,owner,request_id,tpl_id,revision,status,payload,author_name,consent_version,approved_revision,approved_payload,catalog_enabled,catalog_dirty,projection_version,created,updated) VALUES(?,?,?,?,1,'pending',?,'作者','template-sharing-v1',1,?,1,1,1,?,?)",rows)
        with patch.object(m.templates,'upsert_shared') as publishing:
            result=api.reconcile(m);self.assertEqual(result['applied'],201);self.assertEqual(publishing.call_count,201)
        self.assertEqual(len(self.store.visible_template_ids(ids)),201)
        self.assertLessEqual(sum(len(k[1]) for k in self.store._visibility_cache),200)

    def test_share_committed_approval_intent_cannot_change_text_without_new_revision(self):
        row=self.submit(self.ready_share())
        with patch.object(m.templates,'_save_locked',side_effect=OSError('fixture interrupted')):self.assertEqual(self.review(row).status_code,503)
        changed=self.review(row,patch={'prompt':'不同审核文本'});self.assertEqual(changed.status_code,409)
        self.assertEqual(api.reconcile(m)['failed'],0)
        approved=api.owner_view(m,self.store.get(row['id']))
        old=m.templates.get_template(row['template_id']);edited=self.review(approved,patch={'prompt':'不同审核文本'})
        self.assertEqual(edited.status_code,200,edited.text);new=m.templates.get_template(row['template_id'])
        self.assertGreater(new['source_revision'],old['source_revision'])

    def test_share_slow_cos_confirmation_does_not_serialize_another_submission(self):
        from concurrent.futures import ThreadPoolExecutor
        a=self.create(name='慢上传');b=self.create(name='另一投稿');image=self.image();issued=[]
        for row in (a,b):
            response=self.client.post('/api/template-shares/uploads',headers=self.headers,json={'share_id':row['id'],'revision':row['revision'],'ext':'jpg','size':len(image),'content_type':'image/jpeg'})
            self.assertEqual(response.status_code,200,response.text);slot=response.json();issued.append(slot)
            self.media[urlsplit(slot['url']).path.lstrip('/')]={'body':image,'content_type':'image/jpeg','width':480,'height':640}
        started=threading.Event();release=threading.Event();copy_object=api.cos.copy_object.side_effect
        def slow_copy(settings,source,target,**kwargs):
            if a['id'] in target:started.set();release.wait(5)
            return copy_object(settings,source,target,**kwargs)
        def confirm(row,slot):
            return self.client.post('/api/template-shares/uploads/'+slot['upload_token']+'/complete',headers=self.headers,json={'share_id':row['id'],'revision':row['revision']})
        with patch.object(api.cos,'copy_object',side_effect=slow_copy),ThreadPoolExecutor(max_workers=2) as pool:
            slow=pool.submit(confirm,a,issued[0]);self.assertTrue(started.wait(2))
            fast=pool.submit(confirm,b,issued[1])
            try:self.assertEqual(fast.result(timeout=2).status_code,200)
            finally:release.set()
            self.assertEqual(slow.result(timeout=3).status_code,200)


if __name__=='__main__':
    names=[n for n in TemplateShareTests.__dict__ if n.startswith('test_share_')]
    result=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(TemplateShareTests(n) for n in names))
    failed={test._testMethodName for test,_ in result.failures+result.errors}
    (OUTPUT/'template_shares_results.json').write_text(json.dumps({'cases':[{'case':n,'passed':n not in failed} for n in names]},ensure_ascii=False,indent=2),encoding='utf-8')
    for conn in initial_connections:
        try:conn.close()
        except Exception:pass
    print('TEMPLATE_SHARES_SUMMARY total=%d passed=%d failed=%d'%(result.testsRun,result.testsRun-len(failed),len(failed)))
    raise SystemExit(0 if result.wasSuccessful() else 1)
