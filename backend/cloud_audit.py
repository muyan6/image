"""Durable link-only audits. No image bytes or blocking callback waits.

COS callbacks use a private 256-bit capability object key, never exposed in
user APIs. Only an outstanding exact bucket/key can change an audit verdict.
Keep audit-tencent/ private and do not log callback bodies or object URLs.
"""
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit, unquote

import cos_store as cos
import wechat_sec as wx

PREFIX = 'audit-tencent/'
CALLBACK_PATH = '/api/callbacks/cos-audit'


class CloudAudit:
    def __init__(self, runtime):
        self.runtime = runtime
        self.lock = threading.RLock()
        path = Path(runtime().jobs._db_path).parent / 'cloud_audit.db'
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('''CREATE TABLE IF NOT EXISTS audits (
            id TEXT PRIMARY KEY, jid TEXT NOT NULL, stage TEXT NOT NULL,
            source TEXT NOT NULL, size INTEGER NOT NULL, engine TEXT NOT NULL,
            state TEXT NOT NULL, trace TEXT UNIQUE, capability TEXT UNIQUE,
            result INTEGER, deadline REAL NOT NULL, created REAL NOT NULL,
            UNIQUE(jid, stage))''')
        self.db.execute('CREATE TABLE IF NOT EXISTS early (trace TEXT PRIMARY KEY, result INTEGER, created REAL)')
        self.db.commit()

    def close(self):
        self.db.close()

    def row(self, jid, stage):
        with self.lock:
            row = self.db.execute('SELECT * FROM audits WHERE jid=? AND stage=?', (jid, stage)).fetchone()
            return dict(row) if row else None

    def update(self, aid, **fields):
        with self.lock:
            self.db.execute('UPDATE audits SET '+','.join(k+'=?' for k in fields)+' WHERE id=?', (*fields.values(), aid))
            self.db.commit()

    def gate(self, job, key, size, stage):
        m = self.runtime()
        if not m.settings.moderation().get('enabled'):
            return True
        row = self.row(job['id'], stage)
        if row is None:
            engine = 'wechat' if size <= wx.MAX_WECHAT_CHECK_BYTES and wx.wechat_sec_ready(m.settings) else 'cos'
            now = time.time()
            with self.lock:
                self.db.execute('INSERT INTO audits(id,jid,stage,source,size,engine,state,deadline,created) VALUES(?,?,?,?,?,?,?,?,?)',
                                (secrets.token_hex(16), job['id'], stage, key, size, engine, 'new', min(job['deadline'], now+120), now))
                self.db.commit()
            row = self.row(job['id'], stage)
        if row['source'] != key or row['size'] != size:
            raise RuntimeError('审核对象发生变化，本次停止')
        if row['state'] == 'done':
            if row['result'] == 0:
                if row['capability']:
                    m.cleanup.schedule('cos', row['capability'], time.time())
                return True
            if row['result'] in (1, 2):
                if stage == 'input':
                    m.users.refund_job(job['openid'], job['id'])
                    m.users.record_violation(job['openid'], job['id'], 'image', '图片内容未通过安全审核', job['charged_amount'])
                raise ValueError('图片内容未通过安全审核')
            raise RuntimeError('云端安全审核服务异常，本次停止并退回光子')
        if row['engine'] == 'wechat' and row['state'] in ('new', 'submitting'):
            # An interrupted link submission can be sent again: no paid generation.
            self.update(row['id'], state='submitting')
            try:
                trace = wx.submit_image_url(m.settings, cos.presign(m.settings, 'get', key, ttl_seconds=3600), job['openid'])
                with self.lock:
                    early = self.db.execute('SELECT result FROM early WHERE trace=?', (trace,)).fetchone()
                    early_result = early[0] if early else None
                    self.update(row['id'], trace=trace,
                                state=('done' if early_result >= 0 else 'new') if early else 'waiting',
                                engine='cos' if early and early_result < 0 else 'wechat', result=early_result)
                    self.db.execute('DELETE FROM early WHERE trace=?', (trace,))
                    self.db.commit()
            except wx.WechatSecError:
                self.update(row['id'], engine='cos', state='new', deadline=min(job['deadline'], time.time()+300))
        row = self.row(job['id'], stage)
        if row['engine'] == 'cos' and row['state'] in ('new', 'copying'):
            if not 0 < size <= 32*1024*1024:
                raise ValueError('图片超出腾讯云审核大小范围')
            ext = Path(key).suffix.lower() or '.jpg'
            capability = row['capability'] or PREFIX+stage+'/'+secrets.token_hex(32)+ext
            # Persist before copy: callback may arrive before the PUT response.
            self.update(row['id'], capability=capability, state='copying', deadline=min(job['deadline'], time.time()+300))
            m.cleanup.schedule('cos', capability, job['deadline']+3600)
            # A resumed copy first checks existence, preventing duplicate triggers.
            exists = False
            if row['state'] == 'copying':
                try:
                    exists = cos.object_metadata(m.settings, capability)['size'] == size
                except cos.CosError as exc:
                    if exc.status != 404:
                        raise
            if not exists:
                cos.copy_object(m.settings, key, capability, private=True)
            with self.lock:
                current = self.row(job['id'], stage)
                if current['state'] == 'copying':
                    self.update(row['id'], state='waiting')
        with self.lock:
            current = self.row(job['id'],stage)
            m.jobs.update(job['id'], cloud_phase='wait_audit', audit_stage=stage,
                          stage='audit_input' if stage == 'input' else 'audit_output',
                          cloud_next_at=0 if current['state']=='done' else current['deadline'])
        return False

    def wait(self, job):
        row = self.row(job['id'], job['audit_stage'])
        if row is None:
            raise RuntimeError('审核登记丢失，本次停止')
        if row['state'] != 'done' and time.time() >= row['deadline']:
            if row['engine'] == 'wechat':
                self.update(row['id'], engine='cos', state='new', deadline=min(job['deadline'], time.time()+300))
            else:
                raise RuntimeError('云端安全审核服务超时，本次停止并退回光子')
        if row['state'] in ('done','new','copying','submitting') or time.time() >= row['deadline']:
            self.runtime().jobs.update(job['id'], cloud_phase='prepare' if row['stage']=='input' else 'finalize', cloud_next_at=0)
        else:
            self.runtime().jobs.update(job['id'], cloud_next_at=row['deadline'])

    def wechat_callback(self, trace, suggestion):
        result = {'pass':0, 'review':2, 'risky':1, 'risk':1}.get(suggestion, -1)
        with self.lock:
            row = self.db.execute('SELECT * FROM audits WHERE trace=?', (trace,)).fetchone()
            if row:
                job=self.runtime().jobs.get(row['jid'])
                if not job or job.get('deleted_at') or job['status']!='processing':
                    return True
                if row['engine']=='wechat' and row['state']=='waiting':
                    self.update(row['id'], state='done' if result >= 0 else 'new', result=result,
                                engine='wechat' if result >= 0 else 'cos',
                                deadline=row['deadline'] if result >= 0 else min(job['deadline'],time.time()+300))
                    self.wake_job(row['jid'])
                return True
            self.db.execute('DELETE FROM early WHERE created<?', (time.time()-300,))
            self.db.execute('INSERT OR IGNORE INTO early VALUES(?,?,?)', (trace, result, time.time()))
            self.db.commit()
            return False

    def wake_job(self,jid):
        m=self.runtime();job=m.jobs.get(jid)
        if job and job['status']=='processing' and not job.get('deleted_at') and job.get('cloud_phase')=='wait_audit':
            m.jobs.update(jid,cloud_next_at=0)

    def cos_callback(self, body):
        # Official Simple uses code/data; Detail uses EventName/JobsDetail.
        if not isinstance(body, dict):
            return False
        if body.get('EventName') == 'ReviewImage' and isinstance(body.get('JobsDetail'), dict):
            data = body['JobsDetail']
            url, result = data.get('Url'), data.get('Result')
            code = 0 if data.get('State') == 'Success' else -1
        elif isinstance(body.get('data'), dict) and body['data'].get('event') == 'ReviewImage':
            data = body['data']
            url, result, code = data.get('url'), data.get('result'), body.get('code')
        else:
            return False
        try:
            u = urlsplit(str(url or ''))
        except ValueError:
            return False
        tc = self.runtime().settings.tencent()
        host = '%s.cos.%s.myqcloud.com' % (tc.get('cos_bucket'), tc.get('cos_region'))
        if u.scheme != 'https' or u.netloc != host or u.query or u.fragment:
            return False
        key = unquote(u.path.lstrip('/'))
        if not key.startswith(PREFIX):
            return False
        with self.lock:
            row = self.db.execute('SELECT * FROM audits WHERE capability=?', (key,)).fetchone()
            if not row or row['engine'] != 'cos':
                return False
            job = self.runtime().jobs.get(row['jid'])
            if not job or job.get('deleted_at') or job['status'] != 'processing':
                return True
            if row['state'] == 'done':
                return True  # First verdict wins; duplicate delivery is harmless.
            if time.time() > row['deadline']:
                return True
            if code not in (0, '0'):
                result = -1
            elif type(result) is not int or result not in (0, 1, 2):
                return False
            self.update(row['id'], state='done', result=result)
            self.wake_job(row['jid'])
            return True
