"""Run a deterministic, offline review of an isolated current-source copy.

FAIL means the desired invariant was violated; it is a reproduced project bug.
No live settings, credentials, upload files, or databases are loaded.
"""
from pathlib import Path
import ast
import concurrent.futures
import hashlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from unittest.mock import patch

HERE = Path(os.environ.get('REVIEW_OUTPUT', Path(__file__).resolve().parent)).resolve()
HERE.mkdir(parents=True, exist_ok=True)
ROOT = Path(os.environ.get('REVIEW_ROOT', Path(__file__).resolve().parents[2])).resolve()
results = []


def case(name, fn):
    try:
        ok, observed = fn()
        results.append({'case': name, 'passed': bool(ok), 'observed': observed})
        print(('PASS ' if ok else 'FAIL ') + name + ': ' + json.dumps(observed, ensure_ascii=False))
    except Exception as exc:
        results.append({'case': name, 'passed': False, 'harness_error': repr(exc)})
        print('ERROR ' + name + ': ' + repr(exc))


def syntax():
    files = list((ROOT / 'backend').glob('*.py')) + list((ROOT / 'backend' / 'tools').glob('*.py'))
    for file in files:
        ast.parse(file.read_text(encoding='utf-8-sig'), filename=str(file))
    return True, {'python_files': len(files)}


case('python_syntax', syntax)
temp = tempfile.TemporaryDirectory(prefix='sandbox_', dir=HERE)
sandbox = Path(temp.name).resolve()
assert sandbox.parent == HERE and sandbox.name.startswith('sandbox_')
backend = sandbox / 'backend'
backend.mkdir()
for source in (ROOT / 'backend').glob('*'):
    if source.is_file() and source.suffix in {'.py', '.html'}:
        shutil.copy2(source, backend / source.name)
for key in ('FAL_KEY', 'BAIDU_API_KEY', 'BAIDU_SECRET_KEY', 'WX_APPID', 'WX_APP_SECRET',
            'TENCENT_SECRET_ID', 'TENCENT_SECRET_KEY', 'NORMALIZE_LONG_SIDE', 'UPLOAD_DIR'):
    os.environ.pop(key, None)
os.environ['ADMIN_PASSWORD'] = 'isolated-review-password'
os.environ['LOG_LEVEL'] = 'CRITICAL'
os.environ['DATA_DIR'] = str(backend / 'data')
os.environ['UPLOAD_DIR'] = str(backend / 'uploads')
os.environ['JOB_TTL_SECONDS'] = '2592000'
os.environ['ORIGINAL_TTL_SECONDS'] = '86400'
os.environ['WORKERS'] = '4'
os.environ['MAX_UPLOAD_BYTES'] = '26214400'
os.environ['MAX_PIXELS'] = '60000000'
sys.path.insert(0, str(backend))
import requests
from fastapi.testclient import TestClient
from starlette.requests import Request
import cv2
import numpy as np

# Unexpected outbound traffic is a test harness error, never a real network call.
network_guard = patch.object(requests.sessions.Session, 'request', side_effect=AssertionError('NETWORK_DISABLED'))
network_guard.start()
import main as m
initial_connections = [m.users._conn, m.jobs._conn, m.cleanup._conn]
import cos_store
import gateway_ai
import text_overlay
import templates_store
from settings_store import SettingsStore
from user_store import UserStore
from cleanup_store import CleanupStore

client = TestClient(m.app, raise_server_exceptions=False)
valid_jpg = cv2.imencode('.jpg', np.full((64, 96, 3), 120, dtype=np.uint8))[1].tobytes()
valid_png = cv2.imencode('.png', np.full((64, 96, 3), 120, dtype=np.uint8))[1].tobytes()
case_dirs = []


class FixtureGateway:
    """Isolated successful OpenAI response; no real HTTP or local AI fallback."""
    configured = True
    last_cost_cny = .04
    last_cost_usd = None
    last_scale = None
    last_notice = None
    def enhance(self, source, target, **kwargs):
        shutil.copyfile(source, target)


m._get_client = lambda *args, **kwargs: FixtureGateway()


def reset(name):
    d = sandbox / name
    d.mkdir()
    case_dirs.append(d)
    # The offline smoke response comes from an explicit mock gateway; retired
    # local generation is not a production fallback or a test precondition.
    m.settings = SettingsStore(str(d),mutate_default=lambda doc:doc['cloud_pipeline'].update(enabled=False))
    m.users = UserStore(str(d))
    m.jobs = m.JobStore(2592000, 5000, db_path=str(d / 'jobs.db'))
    m.users.ensure_user('sample_user')
    m.users.ensure_user('sample_inviter')
    # 旧账本专项用固定 90/1/3 夹具；新用户正式默认值由独立测试覆盖。
    m.users.set_balance('sample_user', 90)
    m.users.set_balance('sample_inviter', 90)
    m._uploads.clear()
    m.cleanup = CleanupStore(str(d), m.UPLOAD_DIR)
    m.settings.update({'normalize_long_side': 96, 'chain': ['worldcodes'],
                       'providers': {'worldcodes': {'enabled':True,'request_mode':'sync',
                           'base_url':'https://fixture.invalid','api_key':'fixture-gateway'}},
                       'prices': {'light': 1, 'fine': 3},
                       'rewards': {'invite': 30}})
    return d


def request(user='sample_user'):
    return Request({'type': 'http', 'method': 'GET', 'path': '/',
                    'headers': [(b'authorization', ('Bearer '+m.user_token(user)).encode())]})


def headers(user='sample_user'):
    return {'Authorization': 'Bearer ' + m.user_token(user)}


def cos_config():
    m.settings.update({'tencent': {'secret_id': 'fixture-id', 'secret_key': 'fixture-key',
                                  'cos_bucket': 'fixture-123', 'cos_region': 'ap-guangzhou'}})


def register(name, free=False):
    reset(name)
    m.settings.update({'free_mode': free})
    source = Path(m.UPLOAD_DIR) / (name+'.jpg')
    source.write_bytes(valid_jpg)
    with patch.object(m.pool, 'submit'):
        return m._register_job('sample_user', 'fine', 'clear', str(source), '.jpg')


def smoke():
    reset('smoke')
    r = client.post('/api/rescue', headers=headers(), files={'image': ('photo.jpg', valid_jpg, 'image/jpeg')}, data={'quality': 'light'})
    jid = r.json().get('job_id')
    for _ in range(100):
        job = m.jobs.get(jid) if jid else None
        if job and job['status'] != 'processing':
            break
        time.sleep(.02)
    image = cv2.imread(str(Path(m.UPLOAD_DIR)/job['result_file'])) if job else None
    return r.status_code == 200 and job['status'] == 'succeeded' and image is not None, {'http': r.status_code, 'status': job['status'], 'balance': m.users.get_balance('sample_user'), 'shape': list(image.shape) if image is not None else None}


case('configured_gateway_pipeline_smoke', smoke)


def job_owner():
    r = register('job_owner')
    response = client.get('/api/jobs/'+r['job_id'], headers=headers('sample_inviter'))
    return response.status_code == 404, {'other_user_http': response.status_code}


case('job_owner_guard', job_owner)


def private_image():
    r = register('private_image')
    response = client.get(r['orig_url'].split('?')[0])
    return response.status_code in (401, 403, 404), {'anonymous_http': response.status_code, 'bytes': len(response.content)}


case('private_image_owner_guard', private_image)


def restart_free():
    r = register('restart_free', free=True)
    before = m.users.get_balance('sample_user')
    new = m.JobStore(2592000, 5000, db_path=m.jobs._db_path)
    after = m.users.get_balance('sample_user')
    return after == before, {'before': before, 'after': after, 'persisted_price': m.jobs.get(r['job_id'])['price'], 'status': new.get(r['job_id'])['status']}


case('restart_free_job_no_refund', restart_free)


def paid_to_free():
    r = register('paid_to_free')
    before = m.users.get_balance('sample_user')
    m.settings.update({'free_mode': True})
    m._refund_charged('sample_user', r['job_id'], m.jobs.get(r['job_id'])['price'])
    after = m.users.get_balance('sample_user')
    return after == 90, {'after_charge': before, 'after_failure': after, 'expected': 90}


case('paid_job_refund_after_mode_toggle', paid_to_free)


def free_to_paid():
    r = register('free_to_paid', free=True)
    m.settings.update({'free_mode': False})
    m._refund_charged('sample_user', r['job_id'], m.jobs.get(r['job_id'])['price'])
    after = m.users.get_balance('sample_user')
    return after == 90, {'after_failure': after, 'expected': 90}


case('free_job_no_refund_after_mode_toggle', free_to_paid)


def invite_race():
    reset('invite_race')
    code = m.users.get_user('sample_inviter')['invite_code']
    barrier = threading.Barrier(2)
    def submit():
        barrier.wait(timeout=5)
        return client.post('/api/me/invite', headers=headers(), json={'code':code}).status_code
    with concurrent.futures.ThreadPoolExecutor(2) as workers:
        statuses = list(workers.map(lambda _:submit(),range(2)))
    balances = [m.users.get_balance('sample_user'), m.users.get_balance('sample_inviter')]
    return balances == [120,120] and sorted(statuses)==[200,400], {'balances':balances,'statuses':statuses,'invite_bound_rows':m.users.action_count('sample_user','invite_bound')}


case('invite_once_concurrently', invite_race)


def earn_atomic():
    reset('earn_atomic')
    with concurrent.futures.ThreadPoolExecutor(8) as workers:
        out = list(workers.map(lambda _: m.users.earn('sample_user', 'checkin', 10, 1), range(8)))
    return sum(r[0] for r in out) == 1 and m.users.get_balance('sample_user') == 100, {'successful_claims': sum(r[0] for r in out), 'balance': m.users.get_balance('sample_user')}


case('checkin_atomic', earn_atomic)


def video_claim():
    reset('video_claim')
    m.settings.update({'ads': {'rewarded_video_enabled': True, 'rewarded_video_unit_id': 'adunit-fixture'}})
    r = client.post('/api/me/earn', headers=headers(), json={'kind': 'video'})
    return r.status_code != 200, {'without_ad_receipt_http': r.status_code, 'balance': m.users.get_balance('sample_user')}


case('video_requires_completed_ad_proof', video_claim)


def upload_contract():
    reset('upload_contract'); cos_config()
    with patch.object(m, 'cos_presign', return_value='https://fixture.invalid/signed'):
        r = client.post('/api/uploads', headers=headers(), json={'filename': 'photo.png', 'byteSize': m.MAX_UPLOAD_BYTES+1})
    rec = m._uploads.get(r.json().get('upload_id'), {})
    return r.status_code == 413, {'oversize_http': r.status_code, 'stored_ext': rec.get('ext'), 'stored_filename': rec.get('filename')}


case('cos_json_upload_size_contract', upload_contract)


def cos_actual_size():
    reset('cos_actual_size'); cos_config()
    m._uploads['sizefixture'] = {'openid': 'sample_user', 'key': 'fixture', 'ext': '.png', 'created_at': time.time()}
    padded = valid_png + b'\0'*2048
    with patch.object(m, 'MAX_UPLOAD_BYTES', 1024), patch.object(m, 'cos_head', return_value=True), patch.object(m, 'cos_get', return_value=padded), patch.object(m, '_register_job', return_value={'code': 0}):
        r = client.post('/api/rescue/by-upload', headers=headers(), json={'upload_id': 'sizefixture', 'quality': 'light'})
    return r.status_code == 413, {'limit': 1024, 'actual_bytes': len(padded), 'http': r.status_code}


case('cos_actual_bytes_limited', cos_actual_size)


def persist_expiry():
    d=reset('persist_expiry')
    m.jobs.create('abcdef123456', openid='sample_user', status='succeeded', created_at=time.time()-3000000)
    m.jobs._conn.close()
    m.jobs = m.JobStore(2592000, 5000, db_path=str(d/'jobs.db'))
    m.jobs.sweep()
    count=m.jobs._conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]
    return count == 0, {'in_memory': len(m.jobs._data), 'rows_after_reload_and_sweep': count}


case('expired_db_rows_collected', persist_expiry)


def quota_race():
    reset('quota_race')
    m.settings.update({'quota': {'per_minute': 1, 'daily': 1}})
    barrier=threading.Barrier(2)
    orig=m._check_quota
    def synchronized(user):
        outcome=orig(user); barrier.wait(timeout=5); return outcome
    with patch.object(m, '_check_quota', side_effect=synchronized), patch.object(m.pool,'submit'):
        with concurrent.futures.ThreadPoolExecutor(2) as workers:
            futures=[workers.submit(client.post, '/api/rescue', headers=headers(), files={'image': ('x.jpg',valid_jpg,'image/jpeg')}, data={'quality':'light'}) for _ in range(2)]
            statuses=[f.result(timeout=10).status_code for f in futures]
    return statuses.count(200) == 1, {'quota':1, 'statuses':statuses, 'submitted':m.users.jobs_in_window('sample_user',0)}


case('submission_quota_atomic', quota_race)


def cos_error_type():
    reset('cos_error_type'); cos_config()
    with patch.object(cos_store.requests, 'put', side_effect=requests.ConnectionError('fixture offline')):
        try: cos_store.put_object(m.settings,'fixture',valid_jpg)
        except Exception as e:
            return isinstance(e,cos_store.CosError), {'actual_exception':type(e).__name__, 'expected_exception':'CosError'}
    return False, {'actual_exception':'none'}


case('cos_network_failure_normalized', cos_error_type)


def cos_submission_fallback():
    reset('cos_submission_fallback'); cos_config()
    with patch.object(cos_store.requests, 'put', side_effect=requests.ConnectionError('fixture offline')), patch.object(m.pool,'submit'):
        r=client.post('/api/rescue',headers=headers(),files={'image':('photo.jpg',valid_jpg,'image/jpeg')},data={'quality':'light'})
    return r.status_code==200, {'cos_offline_http':r.status_code,'registered_jobs':len(m.jobs._data),'balance':m.users.get_balance('sample_user')}


case('cos_outage_submission_local_fallback', cos_submission_fallback)


def gateway_fallback():
    d=reset('gateway_fallback')
    source=d/'input.jpg'; source.write_bytes(valid_jpg)
    conf=m.settings.provider('worldcodes'); conf['api_key']='fixture'
    gw=gateway_ai.OpenAIImagesEnhance(conf)
    with patch.object(gw,'_request_image_by_url',side_effect=gateway_ai.GatewayError('URL unsupported',status=400,code='HTTP_400')), patch.object(gw,'_request_image',return_value=valid_jpg) as mp:
        try:
            gw.enhance(str(source),str(d/'output.jpg'),image_url='https://fixture.invalid/image')
            status='succeeded'
        except gateway_ai.GatewayError as e:
            status=e.code
    return status=='succeeded' and mp.call_count==1, {'result':status,'multipart_calls':mp.call_count}


case('gateway_url_to_multipart_fallback', gateway_fallback)


def bad_result():
    d=reset('bad_result')
    source=Path(m.UPLOAD_DIR)/'bad_result_source.jpg'; source.write_bytes(valid_jpg)
    with patch.object(m.pool,'submit'):
        created=m._register_job('sample_user','fine','clear',str(source),'.jpg')
    m.settings.update({'chain':['worldcodes']})
    class BadProvider:
        configured=True
        calls=0
        def enhance(self, inp, out, **kwargs):
            self.calls+=1
            Path(out).write_bytes(b'<html>upstream error</html>')
    provider=BadProvider()
    with patch.object(m,'_get_client',return_value=provider), patch.object(m.engine,'process') as local:
        m._run_pipeline(created['job_id'],'fine','clear')
    job=m.jobs.get(created['job_id'])
    out=Path(m.UPLOAD_DIR)/job['result_file']
    # The paid provider already returned. Reject its invalid image and refund;
    # do not mask the failure with another paid generation or a local substitute.
    balance=m.users.get_balance('sample_user')
    okay=(job['status']=='failed' and balance==90 and not out.exists()
          and m._job_media_url(job,'result') is None and provider.calls==1 and not local.called)
    return okay, {'job_status':job['status'],'provider':job.get('provider'),'balance':balance,
                  'invalid_result_delivered':out.exists(),'paid_provider_calls':provider.calls,
                  'local_fallback_calls':local.call_count}


case('invalid_provider_output_rejected', bad_result)


def community_validation():
    reset('community_validation')
    try:
        m.settings.update({'community': {'enabled':True,'items':[{'title':'fixture','likes':'not-a-number'}]}})
        accepted=True
    except ValueError:
        accepted=False
    r=client.get('/api/community') if accepted else None
    return not accepted, {'settings_accepted_bad_likes':accepted,'public_http':r.status_code if r else None}


case('community_likes_validated', community_validation)


def seed_delete():
    reset('seed_delete')
    d=sandbox/'seed_delete'/'templates';d.mkdir()
    t=templates_store.TemplateStore(str(d))
    target=t.list_templates()[0]['id']
    t.delete_template(target)
    before=t.get_template(target) is not None
    reopened=templates_store.TemplateStore(str(d))
    after=reopened.get_template(target) is not None
    return not after, {'template_id':target,'exists_after_delete':before,'exists_after_restart':after}


case('deleted_seed_stays_deleted', seed_delete)


def cjk_font():
    reset('cjk_font')
    blocked = False
    with patch.object(text_overlay,'_FONT_PATH',''):
        try:text_overlay._font(60)
        except RuntimeError:blocked=True
    docker=all('fonts-noto-cjk' in (ROOT/f).read_text(encoding='utf-8') for f in ['Dockerfile','backend/Dockerfile'])
    distinct=bytes(text_overlay._font(60).getmask('甲')) != bytes(text_overlay._font(60).getmask('乙'))
    return blocked and docker and distinct, {'missing_font_rejected':blocked,'docker_installs_cjk_font':docker,'local_distinct_cjk_masks':distinct}


case('docker_cjk_text_supported', cjk_font)


def jpeg_size_validation():
    d=reset('jpeg_size_validation')
    p=d/'input.jpg';p.write_bytes(valid_jpg)
    m._validate_image(str(p))
    return True, {'jpeg_96x64_accepted':True}


case('image_format_smoke', jpeg_size_validation)


def paid_restart_once():
    r=register('paid_restart_once')
    before=m.users.get_balance('sample_user')
    m.jobs._conn.close()
    m.jobs=m.JobStore(2592000,5000,db_path=m.jobs._db_path)
    first=m.users.get_balance('sample_user')
    m.jobs._conn.close()
    m.jobs=m.JobStore(2592000,5000,db_path=m.jobs._db_path)
    second=m.users.get_balance('sample_user')
    return (before,first,second)==(87,90,90), {'after_charge':before,'after_first_restart':first,'after_second_restart':second}


case('paid_restart_refunded_once', paid_restart_once)


def completed_survives_restart():
    r=register('completed_survives_restart')
    m.jobs.update(r['job_id'],status='succeeded',stage='done')
    m.jobs._conn.close()
    m.jobs=m.JobStore(2592000,5000,db_path=m.jobs._db_path)
    job=m.jobs.get(r['job_id'])
    return job['status']=='succeeded' and m.users.get_balance('sample_user')==87, {'reloaded_status':job['status'],'balance':m.users.get_balance('sample_user')}


case('completed_job_persistence', completed_survives_restart)


def original_retention():
    reset('original_retention')
    original=Path(m.UPLOAD_DIR)/'orig_retentionfixture.jpg'
    original.write_bytes(valid_jpg)
    old=time.time()-2*24*3600
    os.utime(original,(old,old))
    m._startup_file_gc()
    kept=original.exists()
    expired=time.time()-31*86400;os.utime(original,(expired,expired));m._startup_file_gc()
    return kept and not original.exists(), {'kept_at_48_hours':kept,'deleted_at_31_days':not original.exists(),'default_ttl_days':m.JOB_TTL_SECONDS//86400}


case('original_retention_30_days', original_retention)


def refund_race():
    r=register('refund_race')
    with concurrent.futures.ThreadPoolExecutor(8) as workers:
        list(workers.map(lambda _:m.users.refund_job('sample_user',r['job_id']),range(8)))
    count=m.users.action_count('sample_user','refund')
    return m.users.get_balance('sample_user')==90 and count==1, {'balance':m.users.get_balance('sample_user'),'refund_rows':count}


case('refund_concurrency_exactly_once',refund_race)


def signed_image():
    r=register('signed_image')
    url=r['orig_url']
    valid=client.get(url).status_code
    tampered=client.get(url.replace('sig=','sig=0')).status_code
    other=client.get(url.split('?')[0],headers=headers('sample_inviter')).status_code
    with patch.object(m.time,'time',return_value=time.time()+601):
        expired=client.get(url).status_code
    return (valid,tampered,other,expired)==(200,403,403,403), {'signed':valid,'tampered':tampered,'other_unsigned':other,'expired':expired}


case('media_signatures_and_expiry',signed_image)


def delete_endpoint():
    r=register('delete_endpoint')
    job=m.jobs.get(r['job_id'])
    wrong=client.delete('/api/my/jobs/'+job['id'],headers=headers('sample_inviter')).status_code
    removed=client.delete('/api/my/jobs/'+job['id'],headers=headers()).status_code
    query=client.get('/api/jobs/'+job['id'],headers=headers()).status_code
    image=client.get(r['orig_url']).status_code
    listed=client.get('/api/my/jobs',headers=headers()).json()['jobs']
    m.jobs._conn.close()
    m.jobs=m.JobStore(2592000,5000,db_path=m.jobs._db_path)
    reloaded=m.jobs.list_for_openid('sample_user')
    return wrong==404 and removed==200 and query==404 and image==404 and not listed and not reloaded and m.users.get_balance('sample_user')==90, {'wrong_owner':wrong,'delete':removed,'query':query,'image':image,'reloaded_visible':len(reloaded),'balance':m.users.get_balance('sample_user')}


case('delete_persists_and_revokes_queued_job',delete_endpoint)


def delete_running():
    r=register('delete_running')
    m.settings.update({'chain':['worldcodes']})
    class Provider:
        configured=True
        def enhance(self,inp,out,**kwargs):
            client.delete('/api/my/jobs/'+r['job_id'],headers=headers())
            Path(out).write_bytes(valid_jpg)
    with patch.object(m,'_get_client',return_value=Provider()):
        m._run_pipeline(r['job_id'],'fine','clear')
    job=m.jobs.get(r['job_id'])
    exists=(Path(m.UPLOAD_DIR)/job['result_file']).exists()
    return job['status']=='failed' and not exists and job.get('cancel_without_refund') and bool(job.get('submitted_at')) and m.users.get_balance('sample_user')==87, {'status':job['status'],'result_exists':exists,'balance':m.users.get_balance('sample_user'),'cancel_after_submit':bool(job.get('cancel_without_refund'))}


case('running_worker_cannot_resurrect_deleted_job',delete_running)


def cleanup_retry():
    d=reset('cleanup_retry');cos_config()
    queue=m.cleanup
    queue.schedule('cos','fixture-object',time.time()-1)
    from cos_store import CosError
    import cleanup_store
    with patch.object(cleanup_store,'delete_object',side_effect=CosError('offline')):
        queue.run(m.settings)
    row=queue._conn.execute('SELECT attempts,next_try FROM cleanup').fetchone()
    queue.close()
    m.cleanup=CleanupStore(str(d),m.UPLOAD_DIR)
    with patch.object(cleanup_store,'delete_object',return_value=None), patch.object(cleanup_store.time,'time',return_value=row[1]+1):
        removed=m.cleanup.run(m.settings)
    remain=m.cleanup._conn.execute('SELECT COUNT(*) FROM cleanup').fetchone()[0]
    return row[0]==1 and removed==1 and remain==0, {'attempts_persisted':row[0],'removed_after_restart':removed,'remaining':remain}


case('cos_cleanup_retry_survives_restart',cleanup_retry)


def video_verified():
    reset('video_verified')
    m.settings.update({'ads':{'rewarded_video_enabled':True,'rewarded_video_unit_id':'adunit-fixture',
                              'rewarded_video_verifier_url':'https://verifier.invalid/verify','rewarded_video_verifier_key':'fixture-key'}})
    import reward_verifier
    def result(user='sample_user',event='event-1',verified=True):
        from unittest.mock import Mock
        response=Mock()
        response.json.return_value={'verified':verified,'openid':user,'ad_unit_id':'adunit-fixture','event_id':event}
        return response
    with patch.object(reward_verifier.requests,'post',return_value=result()):
        first=client.post('/api/me/earn',headers=headers(),json={'kind':'video','receipt':'platform-proof'})
        second=client.post('/api/me/earn',headers=headers(),json={'kind':'video','receipt':'platform-proof'})
    with patch.object(reward_verifier.requests,'post',return_value=result('sample_inviter')):
        mismatch=client.post('/api/me/earn',headers=headers(),json={'kind':'video','receipt':'other-user-proof'})
        cross=client.post('/api/me/earn',headers=headers('sample_inviter'),json={'kind':'video','receipt':'replayed-event'})
    with patch.object(reward_verifier.requests,'post',side_effect=requests.ConnectionError('offline')):
        offline=client.post('/api/me/earn',headers=headers(),json={'kind':'video','receipt':'proof'})
    balance=m.users.get_balance('sample_user')
    return first.status_code==200 and second.status_code==200 and balance==100 and mismatch.status_code==403 and cross.status_code==403 and offline.status_code==403, {'statuses':[first.status_code,second.status_code,mismatch.status_code,cross.status_code,offline.status_code],'balance':balance,'reward_rows':m.users.action_count('sample_user','earn_video')}


case('verified_ad_receipt_binding_replay_and_outage',video_verified)


def cos_stream_limit():
    reset('cos_stream_limit');cos_config()
    from unittest.mock import Mock
    response=Mock();response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
    response.status_code=200;response.headers={};response.iter_content.return_value=[b'x'*600,b'y'*600]
    with patch.object(cos_store.requests,'get',return_value=response):
        try:cos_store.get_object(m.settings,'fixture',max_bytes=1000)
        except cos_store.CosError as e:return e.code=='TOO_LARGE',{'code':e.code,'limit':1000,'stream_bytes':1200}
    return False,{'code':'accepted'}


case('cos_stream_limit_without_content_length',cos_stream_limit)


def failed_persist():
    reset('failed_persist')
    p=Path(m.UPLOAD_DIR)/'persist-failure.jpg';p.write_bytes(valid_jpg)
    with patch.object(m.jobs,'_persist',side_effect=sqlite3.OperationalError('fixture disk failure')):
        try:m._register_job('sample_user','fine','clear',str(p),'.jpg')
        except sqlite3.OperationalError:pass
    return m.users.get_balance('sample_user')==90 and not m.jobs._data, {'balance':m.users.get_balance('sample_user'),'jobs':len(m.jobs._data)}


case('job_persist_failure_refunds_debit',failed_persist)


def recover_orphan_debit():
    reset('recover_orphan_debit')
    m.users.reserve_job('sample_user','abcdef999999',3,{},False)
    before=m.users.get_balance('sample_user')
    m.users.reconcile_charges({})
    m.users.reconcile_charges({})
    return before==87 and m.users.get_balance('sample_user')==90, {'before':before,'after_recovery_twice':m.users.get_balance('sample_user')}


case('orphan_debit_crash_recovery',recover_orphan_debit)


def legacy_community():
    reset('legacy_community')
    m.settings._data['community']={'enabled':True,'items':[None,{'title':'old','likes':'not-a-number'}]}
    response=client.get('/api/community')
    return response.status_code==200 and response.json()['items'][0]['likes']==0, {'status':response.status_code,'likes':response.json()['items'][0]['likes']}


case('legacy_bad_community_data_no_500',legacy_community)


def build_manifest():
    root_docker=(ROOT/'Dockerfile').read_text(encoding='utf-8')
    backend_docker=(ROOT/'backend/Dockerfile').read_text(encoding='utf-8')
    ignores=(ROOT/'.dockerignore').read_text(encoding='utf-8')
    backend_ignores=(ROOT/'backend/.dockerignore').read_text(encoding='utf-8')
    ok='COPY backend/ ./' not in root_docker and 'COPY . ./' not in backend_docker \
        and all(x in ignores for x in ['backend/data','backend/uploads','**/.env']) \
        and all(x in backend_ignores for x in ['data','uploads','.env'])
    return ok, {'allowlisted_copy':ok,'both_contexts_exclude_runtime_data':ok}


case('docker_copy_and_ignore_runtime_data',build_manifest)


def full_queue():
    reset('full_queue')
    m.jobs._max=1
    m.jobs.create('abcdef555555',openid='sample_user',status='processing')
    photo=Path(m.UPLOAD_DIR)/'queue-capacity.jpg';photo.write_bytes(valid_jpg)
    status=None
    with patch.object(m.pool,'submit'):
        try:m._register_job('sample_user','fine','clear',str(photo),'.jpg')
        except m.HTTPException as e:status=e.status_code
    return status==503 and m.users.get_balance('sample_user')==90 and len(m.jobs._data)==1, {'status':status,'balance':m.users.get_balance('sample_user'),'jobs_retained':len(m.jobs._data)}


case('queue_full_does_not_evict_running_or_keep_debit',full_queue)

failed=sum(not r['passed'] for r in results)
print(f'BACKEND_SUMMARY total={len(results)} passed={len(results)-failed} failed={failed}')
(HERE/'backend_results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
network_guard.stop()
client.close()
m.pool.shutdown(wait=True)
m.cleanup.close()
for connection in initial_connections:
    connection.close()
# Windows SQLite handles must be closed before deleting the isolated fixture.
for obj in list(vars(m).values()):
    if isinstance(obj,(m.JobStore,UserStore)):
        try: obj._conn.close()
        except Exception: pass
import gc
gc.collect()
try:
    temp.cleanup()
except PermissionError:
    # A held test connection is harmless; only the isolated sandbox is retained.
    temp._finalizer.detach()
sys.exit(1 if failed else 0)
