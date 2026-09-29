"""Focused community CRUD/visibility/integration checks; isolated data only."""
from pathlib import Path
import io, json, os, shutil, sys, tempfile, unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
output=Path(os.environ.get('REVIEW_OUTPUT',ROOT/'audit/community-tests')).resolve();output.mkdir(parents=True,exist_ok=True)
fixture=tempfile.TemporaryDirectory(prefix='community_',dir=output)
base=Path(fixture.name).resolve();assert base.parent==output
backend=base/'backend';backend.mkdir()
for p in (ROOT/'backend').glob('*'):
    if p.is_file() and p.suffix in ('.py','.html'):shutil.copy2(p,backend/p.name)
for k in ('FAL_KEY','BAIDU_API_KEY','BAIDU_SECRET_KEY','WX_APPID','WX_APP_SECRET','TENCENT_SECRET_ID','TENCENT_SECRET_KEY'):
    os.environ.pop(k,None)
os.environ.update(ADMIN_PASSWORD='community-tests',LOG_LEVEL='CRITICAL',DATA_DIR=str(backend/'data'),UPLOAD_DIR=str(backend/'uploads'))
sys.path.insert(0,str(backend))
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
import requests,templates_store,admin_api
from settings_store import SettingsStore
from community_store import CommunityStore,MEDIA_PREFIX
guard=patch.object(requests.sessions.Session,'request',side_effect=AssertionError('NETWORK_DISABLED'));guard.start()
with patch.object(templates_store.TemplateStore,'ensure_placeholder_covers'):
    import main as m

class CommunityTests(unittest.TestCase):
    def setUp(self):
        d=base/self._testMethodName;d.mkdir()
        self.settings=SettingsStore(str(d));self.store=CommunityStore(self.settings)
        m.settings=self.settings
        app=FastAPI()
        app.include_router(admin_api.make_admin_router(settings=self.settings,announcements=m.announcements,
            templates=m.templates,jobs=m.jobs,users=m.users,health_fn=m.health,stats_fn=lambda:{}))
        self.admin=TestClient(app);self.admin.post('/admin/api/login',json={'password':'community-tests'})
        self.admin.headers.update({'X-Admin-Request':'1'})
        self.public=TestClient(m.app)
    def tearDown(self):self.admin.close();self.public.close()
    def create(self,title='测试帖子',**extra):
        r=self.admin.post('/admin/api/community/posts',json={'title':title,'result_url':'https://example.invalid/photo.jpg','status':'published',**extra})
        self.assertEqual(r.status_code,200,r.text);return r.json()
    def test_pause_resume_are_per_post(self):
        a=self.create('A');b=self.create('B')
        self.assertEqual(self.admin.patch('/admin/api/community/posts/'+a['id'],json={'status':'paused'}).status_code,200)
        self.assertEqual([p['id'] for p in self.public.get('/api/community').json()['items']],[b['id']])
        self.assertEqual(self.admin.get('/admin/api/community/posts?status=paused').json()['total'],1)
        self.admin.patch('/admin/api/community/posts/'+a['id'],json={'status':'published'})
        self.assertEqual(len(self.public.get('/api/community').json()['items']),2)
    def test_delete_is_permanent_and_does_not_return_after_reload(self):
        a=self.create();r=self.admin.delete('/admin/api/community/posts/'+a['id']);self.assertEqual(r.status_code,200)
        reopened=CommunityStore(SettingsStore(str(Path(self.settings._path).parent)))
        self.assertEqual(reopened.list()['total'],0)
        self.assertEqual(self.public.get('/api/community').json()['items'],[])
        self.assertEqual(self.admin.patch('/admin/api/community/posts/'+a['id'],json={'title':'复活'}).status_code,404)
    def test_edit_search_pin_and_sort(self):
        a=self.create('甲',sort=1);b=self.create('乙',sort=50,pinned=True)
        self.assertEqual(self.public.get('/api/community').json()['items'][0]['id'],b['id'])
        self.admin.patch('/admin/api/community/posts/'+a['id'],json={'story':'旅行故事','author_name':'小李','template_name':'胶片'})
        self.assertEqual(self.admin.get('/admin/api/community/posts?q=小李').json()['items'][0]['id'],a['id'])
        self.admin.patch('/admin/api/community/posts/'+b['id'],json={'pinned':False})
        self.assertEqual(self.public.get('/api/community').json()['items'][0]['id'],a['id'])
    def test_batch_pause_resume_and_delete(self):
        ids=[self.create(str(i))['id'] for i in range(3)]
        for action,expected in [('pause',0),('resume',3),('delete',0)]:
            r=self.admin.post('/admin/api/community/posts/batch',json={'ids':ids,'action':action});self.assertEqual(r.status_code,200,r.text)
            self.assertEqual(len(self.public.get('/api/community').json()['items']),expected)
        self.assertEqual(self.store.list()['total'],0)
    def test_bulk_missing_id_is_atomic(self):
        a=self.create();r=self.admin.post('/admin/api/community/posts/batch',json={'ids':[a['id'],'missing'],'action':'delete'})
        self.assertEqual(r.status_code,404);self.assertEqual(self.store.list()['total'],1)
    def test_legacy_migration_stable_ids_and_hidden_posts(self):
        self.settings.update({'community':{'enabled':False,'items':[{'title':'旧帖子','result_url':'https://example.invalid/a.jpg'}]}})
        first=self.store.list()['items'][0];second=self.store.list()['items'][0]
        self.assertEqual(first['id'],second['id']);self.assertEqual(first['status'],'paused')
        self.assertEqual(self.public.get('/api/community').json()['items'],[])
        self.admin.patch('/admin/api/community/posts/'+first['id'],json={'status':'published'})
        self.assertEqual(len(self.public.get('/api/community').json()['items']),1)
    def test_upload_preview_and_unshared_image_delete(self):
        image=io.BytesIO();Image.new('RGB',(48,32),'blue').save(image,'PNG')
        upload=self.admin.post('/admin/api/community/media',files={'file':('a.png',image.getvalue(),'image/png')})
        self.assertEqual(upload.status_code,200,upload.text);url=upload.json()['url']
        self.assertTrue(url.startswith(MEDIA_PREFIX));self.assertEqual(self.public.get(url).status_code,200)
        post=self.create(result_url=url);self.admin.delete('/admin/api/community/posts/'+post['id'])
        self.assertEqual(self.public.get(url).status_code,404)
    def test_shared_image_survives_other_post_delete(self):
        os.makedirs(self.store.media_dir,exist_ok=True);name='a'*32+'.jpg';Path(self.store.media_dir,name).write_bytes(b'owned fixture')
        url=MEDIA_PREFIX+name;a=self.create('A',result_url=url);self.create('B',result_url=url)
        self.admin.delete('/admin/api/community/posts/'+a['id']);self.assertTrue(Path(self.store.media_dir,name).exists())
    def test_auth_and_validation(self):
        with TestClient(self.admin.app) as anonymous:
            self.assertEqual(anonymous.get('/admin/api/community/posts').status_code,403)
        for data in [{'title':''},{'title':'A','status':'published'},{'title':'A','result_url':'javascript:alert(1)','status':'published'}]:
            self.assertEqual(self.admin.post('/admin/api/community/posts',json=data).status_code,400)
        self.assertEqual(self.admin.put('/admin/api/settings',json={'community':{'items':[]}}).status_code,400)
    def test_admin_sidebar_and_editor_replace_json_configuration(self):
        html=(ROOT/'backend/admin.html').read_text(encoding='utf-8')
        self.assertIn('data-page="community"',html);self.assertIn('id="page-community"',html)
        self.assertIn('openCommunityEditor',html);self.assertIn('暂停展示',html);self.assertIn('永久删除',html)
        self.assertNotIn('id="cm-items"',html);self.assertNotIn('id="cm-enabled"',html)

if __name__=='__main__':
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(CommunityTests)
    names=[test._testMethodName for test in suite]
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    failed={test._testMethodName for test,_ in result.failures+result.errors}
    (output/'community_results.json').write_text(json.dumps({'cases':[{'case':name,'passed':name not in failed} for name in names]},indent=2),encoding='utf-8')
    print('COMMUNITY_SUMMARY total=%d passed=%d failed=%d'%(result.testsRun,result.testsRun-len(result.failures)-len(result.errors),len(result.failures)+len(result.errors)))
    m.pool.shutdown(wait=True);m.users.close();m.jobs._conn.close();m.cleanup.close();guard.stop();fixture.cleanup()
    sys.exit(0 if result.wasSuccessful() else 1)
