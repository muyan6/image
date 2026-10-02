"""Consent-bound template drafts and immutable cover grants in users.db.

SQLite is the authority. Catalog JSON is a recoverable approved projection, never
the user's editable draft. Publication intents commit before atomic catalog I/O.
"""
import copy
import json
import re
import threading
import time
import uuid
import weakref
from account_links import init_links, canonical_unlocked, aliases_unlocked, marks

STATES=('draft','pending','rejected','approved','withdrawn')
MAX_COVER_BYTES=8*1024*1024
UPLOAD_TTL=900
PENDING_TTL=7*86400
CONTENT_FIELDS={'name','subtitle','prompt','guide','group_id','cover_tokens'}
GUIDE_FIELDS={'advice','suitable','unsuitable','tips'}
_factory_lock=threading.Lock()


class ShareLimitError(ValueError):pass


def content_fields(raw, *, submit=False):
    if not isinstance(raw,dict) or set(raw)-CONTENT_FIELDS:raise ValueError('投稿字段无效')
    out={}
    for key,maximum in (('name',20),('subtitle',60),('prompt',4000),('group_id',24)):
        value=raw.get(key,'')
        if not isinstance(value,str) or len(value)>maximum:raise ValueError(key+'内容过长或格式无效')
        out[key]=value.strip()
    if out['group_id'] and not re.fullmatch(r'[a-z0-9_]{1,24}',out['group_id']):raise ValueError('分类无效')
    guide=raw.get('guide') or {}
    if not isinstance(guide,dict) or set(guide)-GUIDE_FIELDS:raise ValueError('选图指南字段无效')
    advice=guide.get('advice','')
    if not isinstance(advice,str) or len(advice)>500:raise ValueError('选图建议过长')
    out['guide']={'advice':advice.strip()}
    for key in ('suitable','unsuitable','tips'):
        values=guide.get(key,[])
        if not isinstance(values,list) or len(values)>8 or any(not isinstance(v,str) or len(v)>60 for v in values):raise ValueError('选图指南格式无效（每条最多六十字）')
        out['guide'][key]=[v.strip() for v in values if v.strip()]
    tokens=raw.get('cover_tokens',[])
    if not isinstance(tokens,list) or len(tokens)>3 or any(not isinstance(t,str) or not re.fullmatch(r'[0-9a-f]{32}',t) for t in tokens) or len(set(tokens))!=len(tokens):raise ValueError('请选择最多三张已上传封面')
    out['cover_tokens']=list(tokens)
    if submit and (not out['name'] or not out['prompt'] or not 1<=len(tokens)<=3):raise ValueError('请填写名称、提示词并上传一至三张封面')
    return out


def store_for(users):
    with _factory_lock:
        if not hasattr(users,'_template_shares'):users._template_shares=TemplateShareStore(users)
        return users._template_shares


class TemplateShareStore:
    def __init__(self,users):
        self.users=users;self.flow_lock=threading.RLock();self._visibility_cache={}
        self._row_locks=weakref.WeakValueDictionary();self._row_locks_lock=threading.Lock()
        init_links(users)
        with users._lock,users._conn:
            users._conn.executescript('''
                CREATE TABLE IF NOT EXISTS template_shares(
                    id TEXT PRIMARY KEY,owner TEXT NOT NULL,request_id TEXT NOT NULL,
                    tpl_id TEXT NOT NULL UNIQUE,revision INTEGER NOT NULL,status TEXT NOT NULL,
                    payload TEXT NOT NULL,author_name TEXT NOT NULL,reason TEXT NOT NULL DEFAULT '',
                    consent_version TEXT NOT NULL DEFAULT '',
                    approved_revision INTEGER NOT NULL DEFAULT 0,approved_payload TEXT NOT NULL DEFAULT '{}',
                    catalog_enabled INTEGER NOT NULL DEFAULT 0,catalog_dirty INTEGER NOT NULL DEFAULT 0,
                    projection_version INTEGER NOT NULL DEFAULT 0,applied_revision INTEGER NOT NULL DEFAULT 0,
                    applied_enabled INTEGER NOT NULL DEFAULT 0,published_at REAL NOT NULL DEFAULT 0,
                    created REAL NOT NULL,updated REAL NOT NULL,submitted REAL NOT NULL DEFAULT 0,
                    UNIQUE(owner,request_id));
                CREATE INDEX IF NOT EXISTS template_share_status ON template_shares(status,updated,id);
                CREATE TABLE IF NOT EXISTS template_share_uploads(
                    token TEXT PRIMARY KEY,share_id TEXT NOT NULL,owner TEXT NOT NULL,
                    revision INTEGER NOT NULL,temp_key TEXT NOT NULL,final_key TEXT NOT NULL,
                    ext TEXT NOT NULL,declared_size INTEGER NOT NULL,content_type TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'issued',expires REAL NOT NULL,
                    width INTEGER NOT NULL DEFAULT 0,height INTEGER NOT NULL DEFAULT 0,
                    created REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS template_share_upload_owner ON template_share_uploads(owner,expires);
            ''')

    def row_lock(self,sid):
        """Serialize one submission, not unrelated users' potentially slow COS I/O."""
        with self._row_locks_lock:
            lock=self._row_locks.get(sid)
            if lock is None:lock=threading.RLock();self._row_locks[sid]=lock
            return lock

    def _row(self,sql,args=()):
        cur=self.users._conn.execute(sql,args);row=cur.fetchone()
        if row is None:return None
        out=dict(zip([c[0] for c in cur.description],row))
        for key in ('payload','approved_payload'):
            if key in out:out[key]=json.loads(out[key])
        return out

    def _active(self,owner):
        owner=canonical_unlocked(self.users,owner)
        user=self.users._conn.execute('SELECT banned FROM users WHERE openid=?',(owner,)).fetchone()
        if not user or user[0] or getattr(self.users,'_purging',False):raise ValueError('当前账号暂不能投稿')
        return owner

    def _owned(self,sid,owner):
        row=self._row('SELECT * FROM template_shares WHERE id=?',(sid,))
        if not row or canonical_unlocked(self.users,row['owner'])!=canonical_unlocked(self.users,owner):raise KeyError('投稿不存在')
        return row

    def get(self,sid,owner=None):
        with self.users._lock:
            return self._owned(sid,owner) if owner is not None else self._row('SELECT * FROM template_shares WHERE id=?',(sid,))

    def create(self,owner,request_id,raw,author_name):
        payload=content_fields(raw)
        if not isinstance(request_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,64}',request_id):raise ValueError('投稿请求标识无效')
        now=time.time()
        with self.users._lock,self.users._conn:
            db=self.users._conn;db.execute('BEGIN IMMEDIATE')
            owner=self._active(owner);owners=aliases_unlocked(self.users,owner)
            old=self._row('SELECT * FROM template_shares WHERE owner IN ('+marks(owners)+') AND request_id=?',(*owners,request_id))
            if old:return old
            if payload['cover_tokens']:raise ValueError('请先创建草稿后上传封面')
            if db.execute("SELECT COUNT(*) FROM template_shares WHERE owner IN ("+marks(owners)+") AND status IN ('draft','pending')",owners).fetchone()[0]>=20:raise ShareLimitError('待处理草稿最多二十份')
            sid='ts_'+uuid.uuid4().hex;tid='u_'+sid[3:33]
            db.execute("INSERT INTO template_shares(id,owner,request_id,tpl_id,revision,status,payload,author_name,created,updated) VALUES(?,?,?,?,1,'draft',?,?,?,?)",(sid,owner,request_id,tid,json.dumps(payload,ensure_ascii=False),str(author_name)[:60],now,now))
            return self._row('SELECT * FROM template_shares WHERE id=?',(sid,))

    def _covers(self,row,tokens):
        out=[]
        for token in tokens:
            upload=self._row('SELECT * FROM template_share_uploads WHERE token=? AND share_id=?',(token,row['id']))
            if not upload or upload['status']!='ready' or canonical_unlocked(self.users,upload['owner'])!=canonical_unlocked(self.users,row['owner']):raise ValueError('封面尚未确认或不属于本投稿')
            out.append(upload)
        return out

    def covers(self,row,tokens=None):
        with self.users._lock:return self._covers(row,row['payload']['cover_tokens'] if tokens is None else tokens)

    def update(self,sid,owner,revision,raw,submit=False,consent=False):
        now=time.time()
        if submit and consent is not True:raise ValueError('请确认模板内容与封面的公开分享授权')
        with self.users._lock,self.users._conn:
            db=self.users._conn;db.execute('BEGIN IMMEDIATE');self._active(owner)
            row=self._owned(sid,owner)
            if row['revision']!=revision:raise ValueError('投稿已更新，请刷新后操作')
            if row['status']=='pending':raise ValueError('待审版本请先撤回再修改')
            payload=content_fields({**row['payload'],**raw},submit=submit);self._covers(row,payload['cover_tokens'])
            if submit:
                owners=aliases_unlocked(self.users,owner)
                if db.execute("SELECT COUNT(*) FROM template_shares WHERE owner IN ("+marks(owners)+") AND status='pending' AND id<>?",(*owners,sid)).fetchone()[0]>=5:raise ShareLimitError('待审核投稿最多五份')
            db.execute("UPDATE template_shares SET revision=revision+1,status=?,payload=?,reason='',updated=?,submitted=?,consent_version=? WHERE id=?",('pending' if submit else 'draft',json.dumps(payload,ensure_ascii=False),now,now if submit else 0,'template-sharing-v1' if submit else row['consent_version'],sid))
            return self._row('SELECT * FROM template_shares WHERE id=?',(sid,))

    def issue_upload(self,sid,owner,revision,ext,size,content_type):
        if ext not in ('jpg','png','webp') or type(size) is not int or not 0<size<=MAX_COVER_BYTES:raise ValueError('封面格式或大小无效')
        expected={'jpg':'image/jpeg','png':'image/png','webp':'image/webp'}[ext]
        if content_type!=expected:raise ValueError('封面类型与扩展名不一致')
        now=time.time();token=uuid.uuid4().hex
        with self.users._lock,self.users._conn:
            db=self.users._conn;db.execute('BEGIN IMMEDIATE');self._active(owner);row=self._owned(sid,owner)
            if row['revision']!=revision or row['status']=='pending':raise ValueError('投稿已变更或正在审核')
            owners=aliases_unlocked(self.users,owner)
            if db.execute("SELECT COUNT(*) FROM template_share_uploads WHERE owner IN ("+marks(owners)+") AND status='issued' AND expires>?",(*owners,now)).fetchone()[0]>=8:raise ShareLimitError('未确认上传最多八张')
            temp='uploads/template-shares/'+sid+'/'+token+'.'+ext
            final='template-shares/'+sid+'/covers/'+token+'.'+ext
            db.execute('INSERT INTO template_share_uploads(token,share_id,owner,revision,temp_key,final_key,ext,declared_size,content_type,expires,created) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(token,sid,row['owner'],revision,temp,final,ext,size,expected,now+UPLOAD_TTL,now))
            return self._row('SELECT * FROM template_share_uploads WHERE token=?',(token,))

    def upload(self,token,sid,owner,revision):
        with self.users._lock:
            self._active(owner);row=self._owned(sid,owner)
            upload=self._row('SELECT * FROM template_share_uploads WHERE token=? AND share_id=?',(token,sid))
            if not upload or canonical_unlocked(self.users,upload['owner'])!=canonical_unlocked(self.users,owner):raise KeyError('上传登记不存在')
            if row['revision']!=revision or row['status']=='pending':raise ValueError('投稿版本已变更')
            if upload['status']!='ready' and (upload['revision']!=revision or upload['expires']<=time.time()):raise ValueError('上传登记已过期')
            return upload,row

    def finish_upload(self,token,sid,owner,revision,width,height):
        with self.users._lock,self.users._conn:
            db=self.users._conn;db.execute('BEGIN IMMEDIATE');self._active(owner);row=self._owned(sid,owner)
            upload=self._row('SELECT * FROM template_share_uploads WHERE token=? AND share_id=?',(token,sid))
            if not upload or canonical_unlocked(self.users,upload['owner'])!=canonical_unlocked(self.users,owner):raise KeyError('上传登记不存在')
            if row['revision']!=revision or row['status']=='pending' or upload['status']!='ready' and (upload['revision']!=revision or upload['expires']<=time.time()):raise ValueError('投稿版本或上传登记已变更')
            payload=copy.deepcopy(row['payload']);tokens=payload['cover_tokens']
            if token not in tokens:
                if len(tokens)>=3:raise ValueError('每份投稿最多三张封面')
                tokens.append(token)
            db.execute("UPDATE template_share_uploads SET status='ready',width=?,height=? WHERE token=?",(width,height,token))
            db.execute('UPDATE template_shares SET payload=?,updated=? WHERE id=?',(json.dumps(payload,ensure_ascii=False),time.time(),sid))
            return self._row('SELECT * FROM template_shares WHERE id=?',(sid,))

    def prepare_approve(self,sid,revision,patch=None):
        patch=patch or {}
        if set(patch)-{'name','subtitle','prompt','guide','group_id'}:raise ValueError('审核字段无效')
        now=time.time()
        with self.users._lock,self.users._conn:
            db=self.users._conn;db.execute('BEGIN IMMEDIATE');row=self._row('SELECT * FROM template_shares WHERE id=?',(sid,))
            if not row:raise KeyError('投稿不存在')
            if row['revision']!=revision:raise ValueError('投稿已更新，请刷新后审核')
            self._active(row['owner'])
            if row['status']=='pending' and row['approved_revision']==row['revision'] and row['catalog_enabled']:
                # A committed approval intent is immutable even if catalog I/O
                # was interrupted. Retry it, do not reuse its source revision
                # for different reviewed text while an older version is public.
                if patch and any(row['payload'].get(k)!=v for k,v in patch.items()):
                    raise ValueError('审核版已保存，请先重试发布同步后再修改')
                return row
            if row['status']=='approved' and not patch:return row
            if row['status'] not in ('pending','approved'):raise ValueError('投稿不在待审核状态')
            if row['consent_version']!='template-sharing-v1':raise ValueError('投稿公开授权尚未确认')
            if row['status']=='pending' and row['submitted']+PENDING_TTL<=now:raise ValueError('待审投稿已过期，请作者重新提交')
            payload=content_fields({**row['payload'],**patch},submit=True)
            uploads=self._covers(row,payload['cover_tokens']);approved={**payload,'covers':['cos:'+u['final_key'] for u in uploads]}
            version=row['revision']+int(row['status']=='approved' and bool(patch))
            db.execute("UPDATE template_shares SET revision=?,status='pending',payload=?,approved_revision=?,approved_payload=?,catalog_enabled=1,catalog_dirty=1,projection_version=projection_version+1,published_at=?,reason='',updated=? WHERE id=?",(version,json.dumps(payload,ensure_ascii=False),version,json.dumps(approved,ensure_ascii=False),row['published_at'] or now,now,sid))
            return self._row('SELECT * FROM template_shares WHERE id=?',(sid,))

    def reject(self,sid,revision,reason):
        if not reason.strip():raise ValueError('请填写未通过原因')
        with self.users._lock,self.users._conn:
            db=self.users._conn;db.execute('BEGIN IMMEDIATE');row=self._row('SELECT * FROM template_shares WHERE id=?',(sid,))
            if not row:raise KeyError('投稿不存在')
            if row['revision']!=revision or row['status']!='pending':raise ValueError('投稿状态已变更')
            db.execute("UPDATE template_shares SET status='rejected',reason=?,updated=? WHERE id=?",(reason[:500],time.time(),sid))
            return self._row('SELECT * FROM template_shares WHERE id=?',(sid,))

    def withdraw(self,sid,revision,owner=None,reason=''):
        with self.users._lock,self.users._conn:
            db=self.users._conn;db.execute('BEGIN IMMEDIATE')
            row=self._owned(sid,owner) if owner is not None else self._row('SELECT * FROM template_shares WHERE id=?',(sid,))
            if not row:raise KeyError('投稿不存在')
            if row['revision']!=revision:raise ValueError('投稿已更新，请刷新后操作')
            db.execute("UPDATE template_shares SET status='withdrawn',catalog_enabled=0,catalog_dirty=1,projection_version=projection_version+1,reason=?,updated=? WHERE id=?",(reason[:500],time.time(),sid))
            return self._row('SELECT * FROM template_shares WHERE id=?',(sid,))

    def mark_applied(self,row):
        with self.users._lock,self.users._conn:
            self.users._conn.execute("UPDATE template_shares SET catalog_dirty=0,applied_revision=?,applied_enabled=?,status=CASE WHEN catalog_enabled=1 AND revision=approved_revision THEN 'approved' ELSE status END WHERE id=? AND projection_version=? AND catalog_enabled=?",(row['approved_revision'],row['catalog_enabled'],row['id'],row['projection_version'],row['catalog_enabled']))

    def recovery_rows(self,after_id='',limit=200):
        with self.users._lock:
            cur=self.users._conn.execute('SELECT id FROM template_shares WHERE (catalog_dirty=1 OR catalog_enabled=1) AND id>? ORDER BY id LIMIT ?',(after_id,limit))
            return [self._row('SELECT * FROM template_shares WHERE id=?',(sid,)) for (sid,) in cur.fetchall()]

    def list(self,*,owner=None,status='all',offset=0,limit=24):
        if status!='all' and status not in STATES:raise ValueError('投稿状态无效')
        clauses=[];args=[]
        with self.users._lock:
            if owner is not None:
                owners=aliases_unlocked(self.users,owner);clauses.append('owner IN ('+marks(owners)+')');args.extend(owners)
            if status!='all':clauses.append('status=?');args.append(status)
            where=' WHERE '+' AND '.join(clauses) if clauses else ''
            total=self.users._conn.execute('SELECT COUNT(*) FROM template_shares'+where,args).fetchone()[0]
            ids=self.users._conn.execute('SELECT id FROM template_shares'+where+' ORDER BY updated DESC,id LIMIT ? OFFSET ?',(*args,max(1,min(200,limit)),max(0,offset))).fetchall()
            rows=[self._row('SELECT * FROM template_shares WHERE id=?',(sid,)) for (sid,) in ids]
        return rows,total

    def visible_template_ids(self,ids,enabled=True):
        if not enabled:return set()
        found=set();ids=list(dict.fromkeys(ids))
        with self.users._lock:
            db=self.users._conn;token=(db.total_changes,db.execute('PRAGMA data_version').fetchone()[0])
            self._visibility_cache={k:v for k,v in self._visibility_cache.items() if k[0]==token}
            for start in range(0,len(ids),200):
                chunk=tuple(ids[start:start+200]);key=(token,chunk)
                cached=self._visibility_cache.get(key)
                if cached is None:
                    cached={r[0] for r in db.execute("SELECT s.tpl_id FROM template_shares s LEFT JOIN account_aliases a ON a.alias_openid=s.owner JOIN users u ON u.openid=COALESCE(a.canonical_openid,s.owner) WHERE s.catalog_enabled=1 AND s.applied_enabled=1 AND s.applied_revision>0 AND u.banned=0 AND s.tpl_id IN ("+marks(chunk)+')',chunk)}
                    while self._visibility_cache and sum(len(k[1]) for k in self._visibility_cache)+len(chunk)>200:self._visibility_cache.pop(next(iter(self._visibility_cache)))
                    self._visibility_cache[key]=cached
                found.update(cached)
        return found
