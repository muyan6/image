"""Offline signed thumbnail/catalog/community regressions; fixture stores only."""
import copy
import hashlib
import hmac
import json
import time
import sqlite3
import io
import unittest
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlsplit
from unittest.mock import patch

from test_community_submissions import SubmissionTests, m, cp, OUTPUT, initial_connections
from templates_store import TemplateStore
from community_store import CommunityStore, editorial_thumbnail, MEDIA_PREFIX
import cos_store


def verified_signature(url, secret):
    parsed = urlsplit(url)
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    names = params['q-url-param-list'].split(';') if params['q-url-param-list'] else []
    # COS's list stores URL-encoded, lower-cased names, not decoded raw names.
    selected = sorted((quote(k, safe='').lower(), quote(v, safe=''))
                      for k, v in params.items() if quote(k, safe='').lower() in names)
    query = '&'.join(k + '=' + v for k, v in selected)
    canonical = 'get\n%s\n%s\n\n' % (parsed.path, query)
    key_time = params['q-key-time']
    sign_key = hmac.new(secret.encode(), key_time.encode(), hashlib.sha1).hexdigest()
    text = 'sha1\n%s\n%s\n' % (key_time, hashlib.sha1(canonical.encode()).hexdigest())
    return hmac.new(sign_key.encode(), text.encode(), hashlib.sha1).hexdigest(), params


class LoadMediaTests(SubmissionTests):
    def fill_public(self, count):
        submitted=self.submit();self.assertEqual(self.review(submitted).status_code,200)
        row=self.store.get(submitted['id']);columns=list(row);batch=[]
        for n in range(1,count):
            clone=copy.deepcopy(row);clone.update(id='s_fixture_%04d'%n,job_id='%012x'%n,
                submitted=row['submitted']-n,featured=int(n%3==0))
            clone['payload']['category']='film' if n%2 else 'anime'
            clone['payload']=json.dumps(clone['payload'])
            batch.append(tuple(clone[k] for k in columns))
        with m.users._lock,m.users._conn:
            m.users._conn.executemany('INSERT INTO community_submissions ('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')',batch)
        return submitted

    def catalog(self):
        with patch.object(TemplateStore, 'ensure_placeholder_covers'):
            store = TemplateStore(str(self.d / 'catalog'))
        tid = store.list_templates()[0]['id']
        store.set_cover_slot(tid, 0, 'cos:covers/public_fixture.jpg')
        return store, tid

    def test_load_actual_hmac_binds_processing_and_original_stays_hd(self):
        key = 'results/private/fixture.jpg'
        url = cos_store.thumbnail_url(m.settings, key, ttl_seconds=120)
        signature, params = verified_signature(url, m.settings.tencent()['secret_key'])
        self.assertEqual(signature, params['q-signature'])
        rule = 'imageMogr2/thumbnail/480x480>/format/jpg/quality/70/strip'
        self.assertIn(rule, params)
        self.assertEqual(params[rule], '')
        self.assertEqual(params['q-url-param-list'], quote(rule, safe='').lower())
        start, end = map(int, params['q-sign-time'].split(';'))
        self.assertEqual(end-start, 120)
        self.assertNotIn('imageMogr2', cos_store.presign(m.settings, 'get', key))
        tampered = url.replace('480x480', '960x960')
        digest, altered = verified_signature(tampered, m.settings.tencent()['secret_key'])
        self.assertNotEqual(digest, altered['q-signature'])

    def test_load_thumbnail_has_no_proxy_no_objects_and_preserves_source_hash(self):
        source = self.image((100, 60)); before = hashlib.sha256(source).hexdigest()
        with patch.object(cos_store, 'control_request', side_effect=AssertionError('CONTROL_IO')), \
             patch.object(cos_store, 'get_object', side_effect=AssertionError('IMAGE_PROXY')), \
             patch.object(cos_store, 'put_object', side_effect=AssertionError('NEW_OBJECT')):
            url = cos_store.thumbnail_url(m.settings, 'results/private/fixture.jpg')
        self.assertEqual(hashlib.sha256(source).hexdigest(), before)
        self.assertEqual(urlsplit(url).hostname, cos_store._host(m.settings.tencent()))
        self.assertIn('480x480%3E', url)

    def test_load_thumbnail_rejects_invalid_size_ttl(self):
        for width in (0, 2049, True, '480'):
            with self.assertRaises(ValueError):cos_store.thumbnail_url(m.settings, 'fixture.jpg', width=width)
        for ttl in (0, 7201, True, '300'):
            with self.assertRaises(ValueError):cos_store.thumbnail_url(m.settings, 'fixture.jpg', ttl_seconds=ttl)

    def test_load_catalog_cache_signs_once_hides_prompt_prices_are_fresh(self):
        store, tid = self.catalog()
        with patch.object(cos_store, 'thumbnail_url', wraps=cos_store.thumbnail_url) as thumb:
            first = store.public_templates(m.settings)
            second = store.public_templates(m.settings)
            self.assertEqual(thumb.call_count, 1)
        item = next(t for t in first if t['id']==tid)
        self.assertIn('imageMogr2', item['thumbnail'])
        self.assertNotIn('imageMogr2', item['cover'])
        self.assertEqual(item['covers'][0], item['cover'])
        self.assertFalse(any('prompt' in t or 'model_override' in t for t in first))
        first[0]['name']='client mutation'
        self.assertNotEqual(store.public_templates(m.settings)[0]['name'], 'client mutation')
        m.settings.update({'prices': {'light':21,'fine':73}})
        self.assertTrue(all(t['tier_prices']=={'light':21,'fine':73} for t in store.public_templates(m.settings)))

    def test_load_catalog_crud_disable_cover_and_credentials_invalidate_immediately(self):
        store, tid = self.catalog();first = store.public_templates(m.settings)
        store.set_cover_slot(tid, 0, 'cos:covers/replaced.jpg')
        self.assertIn('/covers/replaced.jpg', next(t for t in store.public_templates(m.settings) if t['id']==tid)['thumbnail'])
        m.settings.update({'tencent': {'cos_custom_domain':'new.cos.invalid'}})
        self.assertEqual(urlsplit(next(t for t in store.public_templates(m.settings) if t['id']==tid)['thumbnail']).hostname,'new.cos.invalid')
        store.update_template(tid, {'enabled':False})
        self.assertFalse(any(t['id']==tid for t in store.public_templates(m.settings)))
        self.assertTrue(any(t['id']==tid for t in first))

    def test_load_catalog_cache_ttl_below_signature_ttl(self):
        store, tid = self.catalog();now=time.time()
        with patch('templates_store.time.time', return_value=now):first=store.public_templates(m.settings)
        with patch('templates_store.time.time', return_value=now+31), \
             patch.object(cos_store, 'thumbnail_url', wraps=cos_store.thumbnail_url) as thumb:
            second=store.public_templates(m.settings)
            self.assertEqual(thumb.call_count,1)
        a=next(t for t in first if t['id']==tid)['thumbnail'];b=next(t for t in second if t['id']==tid)['thumbnail']
        self.assertNotEqual(a,b)

    def test_load_failed_catalog_save_keeps_visible_cache(self):
        store, tid=self.catalog();first=store.public_templates(m.settings)
        with patch.object(store,'_save_locked',side_effect=OSError('fixture disk full')):
            with self.assertRaises(OSError):store.update_template(tid,{'enabled':False})
        self.assertEqual(store.public_templates(m.settings),first)

    def test_load_editorial_cache_narrow_detached_and_pause_is_immediate(self):
        store=CommunityStore(m.settings)
        post=store.create({'title':'editorial','status':'published','result_url':'https://public.invalid/fixture.jpg'})
        with patch.object(m.settings,'snapshot',side_effect=AssertionError('FULL_SETTINGS_COPY')), \
             patch.object(store,'list',wraps=store.list) as listing:
            first=store.public_items();second=store.public_items();self.assertEqual(listing.call_count,1)
        first[0]['title']='client mutation';self.assertEqual(second[0]['title'],'editorial')
        store.update(post['id'],{'status':'paused'})
        self.assertEqual(store.public_items(),[])
        store.update(post['id'],{'status':'published'})
        self.assertEqual(len(store.public_items()),1)
        store.delete(post['id']);self.assertEqual(store.public_items(),[])

    def test_load_public_thumbnail_only_published_withdrawal_and_ban_are_immediate(self):
        pending=self.submit();self.assertEqual(self.feed(),[])
        self.assertEqual(pending.get('thumbnail_url'),'')
        self.assertEqual(self.review(pending).status_code,200)
        public=self.feed()[0]
        self.assertIn('imageMogr2',public['thumbnailUrl'])
        self.assertNotIn('imageMogr2',public['resultUrl'])
        self.assertEqual(public['origUrl'],'')
        m.users.set_banned('sample_user',True)
        self.assertEqual(self.feed(),[])
        m.users.set_banned('sample_user',False)
        self.assertEqual(len(self.feed()),1)
        row=self.store.get(pending['id']);self.assertEqual(self.withdraw(row).status_code,200)
        self.assertEqual(self.feed(),[])

    def test_load_public_cache_never_shares_liked_state_or_counts(self):
        pending=self.submit();self.assertEqual(self.review(pending).status_code,200)
        sid=pending['id'];other={'Authorization':'Bearer '+m.user_token('other_user')}
        a=self.feed(self.headers)[0];b=self.feed(other)[0];self.assertFalse(a['liked']);self.assertFalse(b['liked'])
        liked=self.client.put('/api/community/posts/'+sid+'/like',headers=self.headers,json={'liked':True})
        self.assertEqual(liked.status_code,200)
        a=self.feed(self.headers)[0];b=self.feed(other)[0]
        self.assertTrue(a['liked']);self.assertFalse(b['liked']);self.assertEqual(a['likes'],b['likes'])
        self.assertEqual(self.client.get('/api/community?liked_only=true',headers=other).json()['items'],[])
        from community_interactions import interactions_for
        interaction=interactions_for(m.users)
        with patch.object(interaction,'counts',return_value={sid:3}):self.assertEqual(self.feed()[0]['comments'],3)
        with patch.object(interaction,'counts',return_value={sid:4}):self.assertEqual(self.feed()[0]['comments'],4)

    def test_load_only_page_media_is_signed(self):
        first=self.submit();self.assertEqual(self.review(first).status_code,200)
        self.make_job('fedcba654321');second=self.submit(job_id='fedcba654321');self.assertEqual(self.review(second).status_code,200)
        with patch.object(cos_store,'thumbnail_url',wraps=cos_store.thumbnail_url) as signing:
            feed=self.client.get('/api/community?limit=1').json()
            self.assertEqual(signing.call_count,1)
        self.assertEqual(len(feed['items']),1);self.assertEqual(feed['total'],2);self.assertTrue(feed['has_more'])

    def test_load_large_feed_decodes_only_page_and_bounds_candidate_cache(self):
        self.fill_public(650)
        with patch('community_submissions.json.loads',wraps=json.loads) as decoding:
            ids,total=self.store.public_page([],limit=24)
            rows=self.store.public_rows([p['id'] for p in ids])
            self.assertEqual(decoding.call_count,24)
        self.assertEqual(total,650);self.assertEqual(len(rows),24)
        traces=[];m.users._conn.set_trace_callback(traces.append)
        self.store.public_page([],limit=24)
        m.users._conn.set_trace_callback(None)
        self.assertFalse(any('COUNT(*)' in sql for sql in traces))
        for offset in range(0,240,24):self.store.public_page([],limit=24,offset=offset)
        self.assertLessEqual(len(self.store._public_pages),4)
        self.assertLessEqual(sum(len(v[0]) for v in self.store._public_pages.values()),200)
        with patch.object(self.store,'list',side_effect=AssertionError('FULL_PAYLOAD_SCAN')), \
             patch.object(cos_store,'thumbnail_url',wraps=cos_store.thumbnail_url) as signing:
            response=self.client.get('/api/community?limit=24')
            self.assertEqual(response.status_code,200,response.text);self.assertEqual(signing.call_count,24)

    def test_load_mixed_editorial_submission_categories_pages_and_fallback_match(self):
        self.fill_public(12);editorial=CommunityStore(m.settings)
        first=editorial.create({'title':'pinned','status':'published','result_url':'https://public.invalid/a.jpg','category':'film','pinned':True,'sort':0})
        editorial.create({'title':'unrelated','status':'published','result_url':'https://public.invalid/b.jpg','category':'anime'})
        whole=self.client.get('/api/community?category=film').json()
        a=self.client.get('/api/community?category=film&limit=3').json();b=self.client.get('/api/community?category=film&limit=3&offset=3').json()
        self.assertEqual(a['items'][0]['id'],first['id'])
        self.assertEqual([p['id'] for p in a['items']+b['items']],[p['id'] for p in whole['items'][:6]])
        self.assertTrue(all(p['category']=='film' for p in whole['items']))
        self.store._public_pages.clear();self.store._json_available=False
        fallback=self.client.get('/api/community?category=film').json()
        self.assertEqual([p['id'] for p in fallback['items']],[p['id'] for p in whole['items']])
        self.assertEqual(fallback['total'],whole['total'])

    def test_load_candidate_cache_external_connection_withdraw_ban_and_publish(self):
        pending=self.fill_public(3)
        self.assertEqual(self.client.get('/api/community').json()['total'],3)
        external=sqlite3.connect(m.users._path)
        try:
            with external:external.execute("UPDATE community_submissions SET status='withdrawn' WHERE id=?",(pending['id'],))
            self.assertEqual(self.client.get('/api/community').json()['total'],2)
            with external:external.execute("UPDATE users SET banned=1 WHERE openid='sample_user'")
            self.assertEqual(self.client.get('/api/community').json()['items'],[])
            with external:external.execute("UPDATE users SET banned=0 WHERE openid='sample_user'")
            self.assertEqual(self.client.get('/api/community').json()['total'],2)
            with external:external.execute("UPDATE community_submissions SET status='published' WHERE id=?",(pending['id'],))
            self.assertEqual(self.client.get('/api/community').json()['total'],3)
        finally:external.close()

    def test_load_editorial_cos_exact_host_processing_and_third_party_compatibility(self):
        conf=m.settings.tencent();host=cos_store._host(conf)
        public='https://'+host+'/community/editorial/public_fixture.jpg'
        generated=editorial_thumbnail(m.settings,public)
        digest,params=verified_signature(generated,conf['secret_key'])
        self.assertIn('imageMogr2',generated);self.assertEqual(digest,params['q-signature'])
        for url in ('https://third-party.invalid/image.jpg',public+'?q-signature=unknown',
                    public+'?imageMogr2/thumbnail/640x',public+'?v=operator',
                    'https://'+host+'.evil.invalid/community/editorial/image.jpg'):
            self.assertEqual(editorial_thumbnail(m.settings,url),url)
        m.settings.update({'tencent':{'cos_custom_domain':'custom.cos.invalid'}})
        self.assertIn('imageMogr2',editorial_thumbnail(m.settings,'https://custom.cos.invalid/community/editorial/public_fixture.jpg'))
        self.assertIn('imageMogr2',editorial_thumbnail(m.settings,public))
        store=CommunityStore(m.settings);store.create({'title':'COS宣传','status':'published','result_url':public})
        post=self.feed()[0];self.assertEqual(post['resultUrl'],public);self.assertIn('imageMogr2',post['thumbnailUrl'])

    def test_load_editorial_local_readonly_thumbnail_publication_guard_mtime_and_bounds(self):
        from PIL import Image
        store=CommunityStore(m.settings);path=Path(store.media_dir);path.mkdir(parents=True)
        name='e'*32+'.jpg';source=path/name
        Image.new('RGB',(1280,960),'blue').save(source,'JPEG',quality=90)
        original_hash=hashlib.sha256(source.read_bytes()).hexdigest();ref=MEDIA_PREFIX+name
        variant=editorial_thumbnail(m.settings,ref)
        self.assertIn('/thumbnail?v=',variant)
        # A file just uploaded, or referenced only by paused posts, is private to this variant.
        self.assertEqual(self.client.get(variant).status_code,404)
        row=store.create({'title':'本地宣传','status':'paused','result_url':ref})
        self.assertEqual(self.client.get(variant).status_code,404)
        store.update(row['id'],{'status':'published'})
        post=self.feed()[0];self.assertEqual(post['resultUrl'],ref);self.assertEqual(post['thumbnailUrl'],variant)
        response=self.client.get(variant);self.assertEqual(response.status_code,200)
        self.assertEqual(response.headers['cache-control'],'private, no-store')
        with Image.open(io.BytesIO(response.content)) as thumbnail:
            self.assertEqual(thumbnail.format,'JPEG');self.assertLessEqual(max(thumbnail.size),480)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),original_hash)
        print('LOCAL_EDITORIAL_MEDIA source_bytes=%d thumbnail_bytes=%d max_side=480 source_hash_unchanged=True'%(source.stat().st_size,len(response.content)))
        with patch('PIL.Image.open',side_effect=AssertionError('REPEATED_DECODE')):
            self.assertEqual(self.client.get(variant).content,response.content)
        Image.new('RGB',(1280,960),'red').save(source,'JPEG',quality=90)
        new_variant=editorial_thumbnail(m.settings,ref);self.assertNotEqual(new_variant,variant)
        refreshed=self.client.get(new_variant);self.assertEqual(refreshed.status_code,200);self.assertNotEqual(refreshed.content,response.content)
        m.settings.update({'community':{'enabled':False}})
        with patch('PIL.Image.open',side_effect=AssertionError('DISABLED_COMMUNITY_CACHE_HIT')):
            self.assertEqual(self.client.get(new_variant).status_code,404)
        m.settings.update({'community':{'enabled':True}})
        self.assertEqual(self.client.get(new_variant).status_code,200)
        for n in range(34):
            extra='%032x'%n+'.jpg';Image.new('RGB',(50,40),'blue').save(path/extra,'JPEG')
            store.create({'title':'cache'+str(n),'status':'published','result_url':MEDIA_PREFIX+extra})
            self.assertTrue(store.thumbnail_bytes(extra))
        self.assertLessEqual(len(m.settings._community_thumbnail_cache),32)
        self.assertLessEqual(sum(len(v) for v in m.settings._community_thumbnail_cache.values()),8*1024*1024)
        store.update(row['id'],{'status':'paused'})
        self.assertEqual(self.client.get(new_variant).status_code,404)
        self.assertEqual(self.client.get('/api/community/media/../private.jpg/thumbnail').status_code,404)

    def test_load_local_thumbnail_conversion_does_not_hold_settings_lock_and_rechecks_visibility(self):
        from PIL import Image
        from concurrent.futures import ThreadPoolExecutor
        store=CommunityStore(m.settings);path=Path(store.media_dir);path.mkdir(parents=True)
        name='f'*32+'.jpg';Image.new('RGB',(1280,960),'blue').save(path/name,'JPEG')
        ref=MEDIA_PREFIX+name;store.create({'title':'并发下架','status':'published','result_url':ref})
        opened=Image.open;observed={'settings_write_during_decode':False}
        def pause_during_open(*args,**kwargs):
            pool=ThreadPoolExecutor(max_workers=1)
            try:
                pool.submit(m.settings.update,{'community':{'enabled':False}}).result(timeout=2)
                observed['settings_write_during_decode']=True
            finally:pool.shutdown(wait=False)
            return opened(*args,**kwargs)
        with patch('PIL.Image.open',side_effect=pause_during_open):
            response=self.client.get(ref+'/thumbnail')
        self.assertTrue(observed['settings_write_during_decode'])
        self.assertEqual(response.status_code,404)


if __name__=='__main__':
    if not hasattr(cos_store,'thumbnail_url'):
        for connection in initial_connections:
            try:connection.close()
            except Exception:pass
        print('LOAD_MEDIA_BASELINE thumbnail_url=missing list_images=original no_image_proxy=unchanged')
        raise SystemExit(0)
    names=[n for n in LoadMediaTests.__dict__ if n.startswith('test_load_')]
    suite=unittest.TestSuite(LoadMediaTests(n) for n in names)
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    failed={test._testMethodName for test,_ in result.failures+result.errors}
    (OUTPUT/'load_media_results.json').write_text(json.dumps({'cases':[{'case':n,'passed':n not in failed} for n in names]},indent=2),encoding='utf-8')
    for connection in initial_connections:
        try:connection.close()
        except Exception:pass
    print('LOAD_MEDIA_SUMMARY total=%d passed=%d failed=%d'%(result.testsRun,result.testsRun-len(failed),len(failed)))
    raise SystemExit(0 if result.wasSuccessful() else 1)
