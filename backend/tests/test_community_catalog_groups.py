"""Offline catalog taxonomy and whole-source SQLite pagination regressions."""
import copy
import json
import time
import unittest
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_community_submissions import SubmissionTests,m,OUTPUT,initial_connections
from templates_store import TemplateStore
from community_store import CommunityStore
from community_api import catalog_context
import admin_api


class CatalogGroupTests(SubmissionTests):
    def setUp(self):
        super().setUp()
        with patch.object(TemplateStore,'ensure_placeholder_covers'):m.templates=TemplateStore(str(self.d))
        self.a=m.templates.create_group('实际分组甲');self.b=m.templates.create_group('实际分组乙')
        m.templates.create_template({'id':'catalog_fixture','name':'当前模板','group_id':self.a['id'],'engine':'light',
                                    'price':40,'prompt':'fixture','cover':'https://fixture.invalid/cover.jpg'})
        m.jobs.update(self.job_id,template_id='catalog_fixture',template_name='当前模板')
        catalog_context(m)
        self.admin.close();app=FastAPI();app.include_router(admin_api.make_admin_router(settings=m.settings,announcements=m.announcements,
            templates=m.templates,jobs=m.jobs,users=m.users,health_fn=m.health,stats_fn=lambda:{}))
        self.admin=TestClient(app);self.admin.post('/admin/api/login',json={'password':'workflow-tests'})
        self.admin.headers.update({'X-Admin-Request':'1'})

    def published(self,category='film'):
        row=self.submit(category=category);self.assertEqual(self.review(row).status_code,200)
        return row

    def fill(self,count=211):
        row=self.published();db=m.users._conn;source=db.execute('SELECT * FROM community_submissions WHERE id=?',(row['id'],)).fetchone()
        columns=[r[1] for r in db.execute('PRAGMA table_info(community_submissions)')];source=dict(zip(columns,source))
        with m.users._lock,db:
            db.execute('DELETE FROM community_submissions')
            for index in range(count):
                clone=dict(source,id='fixture_%04d'%index,job_id='job_%04d'%index,submitted=1000+index)
                payload=json.loads(clone['payload']);payload['category']='film' if index%2==0 else self.b['id']
                clone['payload']=json.dumps(payload,ensure_ascii=False)
                db.execute('INSERT INTO community_submissions('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')',
                           [clone[c] for c in columns])

    def test_feed_categories_match_enabled_template_groups_without_hardcoded_tabs(self):
        expected=[{'id':g['id'],'name':g['name']} for g in m.templates.list_groups(enabled_only=True) if g['id']!='all']
        response=self.client.get('/api/community');self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['categories'],expected)
        self.assertFalse(response.json()['legacySemantics'])
        self.assertEqual(response.json()['category_semantics'],'template_group')
        self.assertNotIn('film',{g['id'] for g in response.json()['categories']})
        mine=self.client.get('/api/community/submissions/mine',headers=self.headers)
        self.assertEqual(mine.json()['categories'],expected)

    def test_legacy_payload_projects_current_group_in_feed_detail_and_owner_without_mutation(self):
        row=self.published('film');before=copy.deepcopy(self.store.get(row['id'])['payload'])
        post=self.client.get('/api/community').json()['items'][0]
        self.assertEqual((post['category'],post['categoryName'],post['group_id']),
                         (self.a['id'],self.a['name'],self.a['id']))
        self.assertEqual(post['legacyCategory'],'film')
        detail=self.client.get('/api/community/posts/'+row['id']).json()['post']
        self.assertEqual(detail['categoryName'],self.a['name'])
        owner=self.client.get('/api/community/submissions/mine',headers=self.headers).json()['items'][0]
        self.assertEqual(owner['category'],'film');self.assertEqual(owner['group_name'],self.a['name'])
        self.assertEqual(self.store.get(row['id'])['payload'],before)

    def test_explicit_current_group_wins_and_admin_namespace_has_real_catalog(self):
        row=self.published(self.b['id'])
        feed=self.client.get('/api/community?category='+self.b['id']).json()
        self.assertEqual(feed['total'],1);self.assertEqual(feed['items'][0]['group_id'],self.b['id'])
        self.assertEqual(self.client.get('/api/community?category='+self.a['id']).json()['total'],0)
        response=self.admin.get('/admin/api/community/posts');self.assertEqual(response.status_code,200,response.text)
        post=next(p for p in response.json()['items'] if p['id']==row['id'])
        self.assertEqual(post['category_name'],self.b['name']);self.assertEqual(post['group_id'],self.b['id'])
        review=self.admin.get('/admin/api/community/submissions?status=published')
        self.assertEqual(review.status_code,200,review.text)
        self.assertEqual(review.json()['items'][0]['categoryName'],self.b['name'])

    def test_rename_move_disable_restore_catalog_changes_are_immediate_and_unmapped_stays_all(self):
        row=self.published('film')
        m.templates.update_group(self.a['id'],{'name':'新名称'})
        self.assertEqual(self.client.get('/api/community').json()['items'][0]['categoryName'],'新名称')
        m.templates.update_template('catalog_fixture',{'group_id':self.b['id']})
        self.assertEqual(self.client.get('/api/community').json()['items'][0]['group_id'],self.b['id'])
        m.templates.update_group(self.b['id'],{'enabled':False})
        post=self.client.get('/api/community').json()['items'][0]
        self.assertEqual((post['category'],post['group_name']),('all','未分组'))
        self.assertEqual(post['id'],row['id'])
        self.assertNotIn(self.b['id'],{g['id'] for g in self.client.get('/api/community').json()['categories']})
        m.templates.update_group(self.b['id'],{'enabled':True})
        self.assertEqual(self.client.get('/api/community').json()['items'][0]['group_id'],self.b['id'])

    def test_old_client_legacy_filter_is_labeled_and_paginated_before_likes(self):
        self.fill()
        feed=self.client.get('/api/community?category=film&limit=24').json()
        self.assertTrue(feed['legacySemantics']);self.assertEqual(feed['category_semantics'],'legacy')
        self.assertEqual(feed['total'],106);self.assertEqual(len(feed['items']),24)
        self.assertTrue(all(p['category']=='film' and p['group_id']==self.a['id'] for p in feed['items']))
        self.assertEqual(self.client.get('/api/community?category=invalid').status_code,400)

    def test_sql_filter_counts_and_pages_cover_all_211_not_only_first_page(self):
        self.fill();seen=[];offset=0
        while True:
            response=self.client.get('/api/community?category='+self.a['id']+'&limit=24&offset='+str(offset))
            self.assertEqual(response.status_code,200,response.text);feed=response.json()
            self.assertEqual(feed['total'],106);self.assertTrue(all(p['group_id']==self.a['id'] for p in feed['items']))
            seen.extend(p['id'] for p in feed['items']);offset=feed['next_offset']
            if not feed['has_more']:break
        self.assertEqual(len(seen),106);self.assertEqual(len(set(seen)),106)
        self.assertEqual(self.client.get('/api/community?limit=24').json()['total'],211)
        self.store._public_pages.clear()
        with patch('community_submissions.json.loads',wraps=json.loads) as decoded:
            result=self.client.get('/api/community?category='+self.a['id']+'&limit=24').json()
        self.assertEqual(len(result['items']),24)
        # Decoding is limited to the selected source page (plus HTTP JSON parse).
        self.assertLessEqual(decoded.call_count,25)

    def test_sql_and_no_json_compatibility_have_same_full_source_group_pages(self):
        self.fill();url='/api/community?category='+self.b['id']+'&limit=17&offset=24'
        sql=self.client.get(url).json();self.store._json_available=False;self.store._public_pages.clear()
        fallback=self.client.get(url).json()
        self.assertEqual(sql['total'],105);self.assertEqual(fallback['total'],105)
        self.assertEqual([p['id'] for p in sql['items']],[p['id'] for p in fallback['items']])

    def test_historical_editorial_above_200_is_not_silently_cut_and_stories_stay_intact(self):
        with m.settings._lock:
            m.settings._data['community']={'enabled':True,'posts_version':1,'items':[
                {'id':'legacy_%04d'%n,'title':'历史'+str(n),'story':'原故事'+str(n),'status':'published','pinned':False,
                 'sort':n,'created_at':1000+n,'category':'film','template_id':'catalog_fixture','result_url':'https://public.invalid/a.jpg'}
                for n in range(211)]}
        feed=self.client.get('/api/community?limit=24&offset=200').json()
        self.assertEqual(feed['total'],211);self.assertEqual(len(feed['items']),11)
        self.assertEqual(feed['items'][0]['story'],'原故事200')
        self.assertTrue(all(p['group_id']==self.a['id'] for p in feed['items']))

    def test_deleted_editorial_does_not_remove_media_still_referenced_after_row_200(self):
        from pathlib import Path
        store=CommunityStore(m.settings);filename='a'*32+'.jpg';url='/api/community/media/'+filename
        media=Path(store.media_dir);media.mkdir(parents=True,exist_ok=True);image=media/filename;image.write_bytes(self.image())
        with m.settings._lock:
            m.settings._data['community']={'enabled':True,'posts_version':1,'items':[
                {'id':'reference_%04d'%n,'title':'保留'+str(n),'story':'原故事','status':'published','pinned':False,
                 'sort':n,'created_at':n+1,'category':'all','result_url':url if n in (0,201) else 'https://public.invalid/a.jpg'}
                for n in range(202)]}
        # Exercise the physical-removal step against an already committed legacy
        # deletion; the normal 200-post editing cap is deliberately unchanged.
        with m.settings._lock:
            removed=m.settings._data['community']['items'].pop(0)
        store._cleanup_deleted_media([removed])
        self.assertTrue(image.is_file());self.assertEqual(len(store.public_items()),201)

    def test_inactive_unchanged_old_submission_and_editorial_category_can_be_saved(self):
        row=self.published(self.a['id']);self.assertEqual(self.withdraw(row).status_code,200)
        m.templates.update_template('catalog_fixture',{'group_id':self.b['id']})
        m.templates.delete_group(self.a['id'])
        repost=self.post(category=self.a['id']);self.assertEqual(repost.status_code,200,repost.text)
        self.assertEqual(repost.json()['submission']['category'],self.a['id'])
        self.assertEqual(repost.json()['submission']['group_id'],self.b['id'])
        editorial=CommunityStore(m.settings)
        post=editorial.create({'title':'旧稿','category':self.b['id'],'result_url':'https://public.invalid/a.jpg'})
        m.templates.update_group(self.b['id'],{'enabled':False})
        changed=editorial.update(post['id'],{'title':'只修改标题'})
        self.assertEqual(changed['category'],self.b['id'])
        with self.assertRaises(ValueError):editorial.create({'title':'新稿','category':self.b['id']})


if __name__=='__main__':
    names=[name for name in CatalogGroupTests.__dict__ if name.startswith('test_')]
    result=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(CatalogGroupTests(name) for name in names))
    failed={test._testMethodName for test,_ in result.failures+result.errors}
    (OUTPUT/'community_catalog_group_results.json').write_text(json.dumps({'cases':[{'case':name,'passed':name not in failed} for name in names]},
        ensure_ascii=False,indent=2),encoding='utf-8')
    for connection in initial_connections:
        try:connection.close()
        except Exception:pass
    print('COMMUNITY_CATALOG_GROUP_SUMMARY total=%d passed=%d failed=%d'%(result.testsRun,result.testsRun-len(failed),len(failed)))
    raise SystemExit(0 if result.wasSuccessful() else 1)
