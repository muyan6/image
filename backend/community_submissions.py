"""User submissions, likes and one-time curation rewards in the balance database."""
import json
import copy
import sqlite3
import threading
import time
import uuid
from account_links import init_links, canonical_unlocked, aliases_unlocked, marks, adjust_balance_unlocked

PENDING_TTL = 7 * 86400
_factory_lock = threading.Lock()


def store_for(users):
    with _factory_lock:
        if not hasattr(users, '_community_submissions'):
            users._community_submissions = SubmissionStore(users)
        return users._community_submissions


class SubmissionStore:
    def __init__(self, users):
        self.users = users
        init_links(users)
        self.flow_lock = threading.RLock()
        with users._lock, users._conn:
            users._conn.executescript('''
                CREATE TABLE IF NOT EXISTS community_submissions(
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, job_id TEXT NOT NULL,
                    revision INTEGER NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL,
                    created REAL NOT NULL, updated REAL NOT NULL, submitted REAL NOT NULL,
                    lease_until REAL NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT '',
                    featured INTEGER NOT NULL DEFAULT 0, reward INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(owner,job_id));
                CREATE INDEX IF NOT EXISTS community_submission_status ON community_submissions(status,submitted);
                CREATE INDEX IF NOT EXISTS community_submission_public_order ON community_submissions(status,featured DESC,submitted DESC,id);
                CREATE TABLE IF NOT EXISTS community_submission_events(owner TEXT, at REAL);
                CREATE INDEX IF NOT EXISTS community_submission_rate ON community_submission_events(owner,at);
                CREATE TABLE IF NOT EXISTS community_likes(post_id TEXT, owner TEXT, PRIMARY KEY(post_id,owner));
                CREATE TABLE IF NOT EXISTS community_rewards(post_id TEXT PRIMARY KEY, owner TEXT, amount INTEGER, at REAL);
            ''')
        self._public_pages = {}
        self._json_available = None

    def _row(self, sql, args=()):
        cursor = self.users._conn.execute(sql, args)
        row = cursor.fetchone()
        if not row:
            return None
        result = dict(zip([c[0] for c in cursor.description], row))
        result['payload'] = json.loads(result['payload'])
        return result

    def get(self, sid):
        with self.users._lock:
            return self._row('SELECT * FROM community_submissions WHERE id=?', (sid,))

    def reserve(self, owner, job, content):
        now = time.time()
        with self.users._lock, self.users._conn:
            db = self.users._conn
            db.execute('BEGIN IMMEDIATE')
            owners=aliases_unlocked(self.users,owner);owner=canonical_unlocked(self.users,owner)
            user = db.execute('SELECT banned FROM users WHERE openid=?', (owner,)).fetchone()
            if not user or user[0] or self.users._purging:
                raise ValueError('当前账号暂不能投稿')
            old = self._row('SELECT * FROM community_submissions WHERE owner IN ('+marks(owners)+') AND job_id=?', (*owners, job['id']))
            if old and old['status'] in ('uploading', 'pending', 'published'):
                if any(old['payload'].get(k) != content[k] for k in ('title','story','category','share_original')):
                    raise ValueError('该作品已投稿；修改公开内容前请先撤回原投稿')
                retry = old['status'] == 'uploading' and old['lease_until'] <= now
                if retry:
                    db.execute('UPDATE community_submissions SET lease_until=? WHERE id=?', (now+180, old['id']))
                return old, retry
            db.execute('DELETE FROM community_submission_events WHERE at<?', (now-86400,))
            count = db.execute('SELECT COUNT(*) FROM community_submission_events WHERE owner IN ('+marks(owners)+') AND at>?', (*owners, now-3600)).fetchone()[0]
            pending = db.execute("SELECT COUNT(*) FROM community_submissions WHERE owner IN ("+marks(owners)+") AND status IN ('uploading','pending')", owners).fetchone()[0]
            if count >= 5 or pending >= 5:
                raise ValueError('每小时最多投稿 5 次，待处理投稿最多 5 件')
            sid = old['id'] if old else 's_'+uuid.uuid4().hex
            revision = old['revision']+1 if old else 1
            prefix = 'community/submissions/'+sid+'/v'+str(revision)+'/'
            source_orig = (job.get('comparison_cos') or job.get('orig_cos')) if content['share_original'] else ''
            ext = (source_orig or '').rsplit('.', 1)[-1].lower()
            if ext not in ('jpg','jpeg','png','webp','bmp','tif','tiff'):ext='jpg'
            payload = {**content, 'source_result':job['result_cos'], 'source_orig':source_orig,
                       'result_key':prefix+'result.jpg', 'orig_key':prefix+'original.'+ext if source_orig else '',
                       'template_id':job.get('template_id') or '', 'template_name':job.get('template_name') or '',
                       'quality':job.get('quality') or 'light', 'consent_version':'community-v1'}
            raw = json.dumps(payload, ensure_ascii=False)
            if old:
                db.execute("UPDATE community_submissions SET revision=?,status='uploading',payload=?,updated=?,submitted=?,lease_until=?,reason='',featured=0 WHERE id=?",
                           (revision,raw,now,now,now+180,sid))
            else:
                db.execute("INSERT INTO community_submissions(id,owner,job_id,revision,status,payload,created,updated,submitted,lease_until) VALUES(?,?,?,?,'uploading',?,?,?,?,?)",
                           (sid,owner,job['id'],revision,raw,now,now,now,now+180))
            db.execute('INSERT INTO community_submission_events VALUES(?,?)', (owner,now))
            return self._row('SELECT * FROM community_submissions WHERE id=?',(sid,)), True

    def finish(self, sid, revision):
        with self.users._lock, self.users._conn:
            changed = self.users._conn.execute("UPDATE community_submissions SET status='pending',lease_until=0,updated=? WHERE id=? AND revision=? AND status='uploading'",
                                              (time.time(),sid,revision)).rowcount
            return bool(changed)

    def release(self, sid, revision):
        with self.users._lock, self.users._conn:
            self.users._conn.execute("UPDATE community_submissions SET lease_until=0 WHERE id=? AND revision=? AND status='uploading'",(sid,revision))

    def withdraw(self, sid, owner, revision):
        with self.users._lock, self.users._conn:
            owners=aliases_unlocked(self.users,owner)
            row = self._row('SELECT * FROM community_submissions WHERE id=? AND owner IN ('+marks(owners)+')',(sid,*owners))
            if not row:raise KeyError('投稿不存在')
            if row['revision'] != revision:raise ValueError('投稿已更新，请刷新后操作')
            self.users._conn.execute("UPDATE community_submissions SET status='withdrawn',featured=0,updated=? WHERE id=?",(time.time(),sid))
            return row

    def review(self, sid, revision, action, reason='', reward=0):
        now=time.time()
        with self.users._lock, self.users._conn:
            db=self.users._conn;db.execute('BEGIN IMMEDIATE')
            row=self._row('SELECT * FROM community_submissions WHERE id=?',(sid,))
            if not row:raise KeyError('投稿不存在')
            if row['revision']!=revision:raise ValueError('投稿版本已更新，请刷新后审核')
            recipient=canonical_unlocked(self.users,row['owner'])
            user=db.execute('SELECT banned FROM users WHERE openid=?',(recipient,)).fetchone()
            if action=='approve':
                if not user or user[0]:raise ValueError('作者账号异常，暂不发布')
                if row['status']=='published':return row
                if row['status']!='pending' or row['submitted']+PENDING_TTL<=now:raise ValueError('投稿已撤回、过期或不在待审核状态')
                if db.execute("SELECT COUNT(*) FROM community_submissions WHERE status='published'").fetchone()[0]>=200:raise ValueError('已发布投稿达到 200 篇，请先下架部分投稿')
                db.execute("UPDATE community_submissions SET status='published',reason='',updated=? WHERE id=?",(now,sid))
            elif action in ('reject','unpublish'):
                expected='pending' if action=='reject' else 'published'
                if row['status']!=expected:raise ValueError('投稿状态已变更，请刷新后操作')
                if not reason.strip():raise ValueError('请填写未通过或下架原因')
                db.execute("UPDATE community_submissions SET status='rejected',featured=0,reason=?,updated=? WHERE id=?",(reason[:500],now,sid))
            elif action in ('feature','unfeature'):
                if row['status']!='published':raise ValueError('只有已发布投稿可以设置精选')
                if action=='feature':
                    if not user or user[0] or self.users._purging:raise ValueError('作者账号异常，暂停发奖')
                    if type(reward) is not int or reward<0:raise ValueError('精选奖励配置无效')
                    inserted=db.execute('INSERT OR IGNORE INTO community_rewards VALUES(?,?,?,?)',(sid,row['owner'],reward,now)).rowcount
                    if inserted:
                        adjust_balance_unlocked(self.users,recipient,reward)
                        db.execute('UPDATE community_submissions SET reward=? WHERE id=?',(reward,sid))
                        db.execute('INSERT INTO audit(ts,openid,action,detail) VALUES(?,?,?,?)',(now,row['owner'],'community_featured','post='+sid+' +='+str(reward)))
                db.execute('UPDATE community_submissions SET featured=?,updated=? WHERE id=?',(int(action=='feature'),now,sid))
            else:raise ValueError('审核操作无效')
            return self._row('SELECT * FROM community_submissions WHERE id=?',(sid,))

    def list(self, *, owner=None, status='all', offset=0, limit=50):
        if status not in ('all','uploading','pending','published','rejected','withdrawn'):raise ValueError('投稿状态无效')
        clauses=[];args=[]
        if owner is not None:
            with self.users._lock:owners=aliases_unlocked(self.users,owner)
            clauses.append('owner IN ('+marks(owners)+')');args.extend(owners)
        if status!='all':clauses.append('status=?');args.append(status)
        where=' WHERE '+' AND '.join(clauses) if clauses else ''
        with self.users._lock:
            total=self.users._conn.execute('SELECT COUNT(*) FROM community_submissions'+where,args).fetchone()[0]
            cur=self.users._conn.execute('SELECT * FROM community_submissions'+where+' ORDER BY submitted DESC,id LIMIT ? OFFSET ?',(*args,min(200,max(1,limit)),max(0,offset)))
            cols=[c[0] for c in cur.description];rows=[dict(zip(cols,row)) for row in cur.fetchall()]
        for row in rows:row['payload']=json.loads(row['payload'])
        return rows,total

    def public_page(self, editorial, *, offset=0, limit=24, category='all', liked_viewer=None, cos_ready=True):
        """Merge at most 200 editorial candidates with indexed public submissions.

        Only a bounded page of IDs/sort metadata is cached. SQLite source tokens
        include both local writes and other connections, including bans/aliases.
        No payloads, signatures, likes or comments are shared by this cache.
        """
        offset=max(0,int(offset));limit=max(1,min(200,int(limit)))
        editorial=tuple((p['id'],int(bool(p.get('pinned'))),int(p.get('sort',100)),float(p.get('created_at') or 0))
                        for p in editorial[:200])
        with self.users._lock:
            db=self.users._conn
            token=(db.total_changes,db.execute('PRAGMA data_version').fetchone()[0])
            viewer=canonical_unlocked(self.users,liked_viewer) if liked_viewer else None
            key=(token,editorial,offset,limit,category,viewer,bool(cos_ready))
            cached=self._public_pages.get(key)
            if cached is not None:return copy.deepcopy(cached)
            if self._json_available is None:
                try:
                    db.execute("SELECT json_extract('{\"category\":\"all\"}', '$.category')").fetchone()
                    self._json_available=True
                except sqlite3.OperationalError:
                    self._json_available=False
            if not self._json_available:
                # Compatibility for custom SQLite builds without JSON1. This
                # correctness path is source-checked too; no dataset is truncated.
                rows=db.execute("SELECT s.id,s.featured,s.submitted,s.payload FROM community_submissions s "
                    "LEFT JOIN account_aliases a ON a.alias_openid=s.owner "
                    "JOIN users u ON u.openid=COALESCE(a.canonical_openid,s.owner) "
                    "WHERE s.status='published' AND u.banned=0").fetchall() if cos_ready else []
                selected=[(eid,pinned,sort,submitted,'editorial') for eid,pinned,sort,submitted in editorial]
                for sid,featured,submitted,payload in rows:
                    p=json.loads(payload)
                    if not p.get('result_key') or category!='all' and p.get('category','all')!=category:continue
                    if viewer and not db.execute("SELECT 1 FROM community_likes l LEFT JOIN account_aliases a ON a.alias_openid=l.owner WHERE l.post_id=? AND COALESCE(a.canonical_openid,l.owner)=?",(sid,viewer)).fetchone():continue
                    selected.append((sid,featured,100,submitted,'submission'))
                selected.sort(key=lambda p:(not p[1],p[2],-p[3],p[0]))
                total=len(selected);selected=selected[offset:offset+limit]
            else:
                values=','.join('(?,?,?,?)' for _ in editorial)
                prefix=("WITH editorial(id,pinned,sort,submitted) AS (VALUES "+values+") " if editorial else
                        "WITH editorial(id,pinned,sort,submitted) AS (SELECT '',0,0,0 WHERE 0) ")
                args=[value for row in editorial for value in row]
                condition="s.status='published' AND u.banned=0 AND ? AND COALESCE(json_extract(s.payload,'$.result_key'),'')<>''"
                args.append(int(bool(cos_ready)))
                if category!='all':condition+=" AND json_extract(s.payload,'$.category')=?";args.append(category)
                if viewer:
                    condition+=" AND EXISTS(SELECT 1 FROM community_likes l LEFT JOIN account_aliases la ON la.alias_openid=l.owner WHERE l.post_id=s.id AND COALESCE(la.canonical_openid,l.owner)=?)"
                    args.append(viewer)
                candidates=("SELECT id,pinned,sort,submitted,'editorial' AS source FROM editorial UNION ALL "
                    "SELECT s.id,s.featured,100,s.submitted,'submission' FROM community_submissions s "
                    "LEFT JOIN account_aliases a ON a.alias_openid=s.owner "
                    "JOIN users u ON u.openid=COALESCE(a.canonical_openid,s.owner) WHERE "+condition)
                total=db.execute(prefix+'SELECT COUNT(*) FROM ('+candidates+')',args).fetchone()[0]
                selected=db.execute(prefix+candidates+' ORDER BY pinned DESC,sort ASC,submitted DESC,id ASC LIMIT ? OFFSET ?',(*args,limit,offset)).fetchall()
            result=([{'id':r[0],'source':r[4]} for r in selected],total)
            # Keep four pages and 200 narrow candidates at most; stale versions
            # are evicted rather than retaining withdrawn identifiers forever.
            self._public_pages={k:v for k,v in self._public_pages.items() if k[0]==token}
            while self._public_pages and (len(self._public_pages)>=4 or
                    sum(len(v[0]) for v in self._public_pages.values())+len(result[0])>200):
                self._public_pages.pop(next(iter(self._public_pages)))
            self._public_pages[key]=result
            return copy.deepcopy(result)

    def public_rows(self, ids):
        """Decode only selected page payloads; recheck publication and author state."""
        ids=list(dict.fromkeys(ids))[:200]
        if not ids:return {}
        with self.users._lock:
            cur=self.users._conn.execute("SELECT s.* FROM community_submissions s LEFT JOIN account_aliases a ON a.alias_openid=s.owner JOIN users u ON u.openid=COALESCE(a.canonical_openid,s.owner) WHERE s.status='published' AND u.banned=0 AND s.id IN ("+marks(ids)+")",ids)
            cols=[c[0] for c in cur.description];rows=[dict(zip(cols,row)) for row in cur.fetchall()]
        for row in rows:row['payload']=json.loads(row['payload'])
        return {row['id']:row for row in rows}

    def rewarded(self, sid):
        with self.users._lock:
            return self.users._conn.execute('SELECT 1 FROM community_rewards WHERE post_id=?',(sid,)).fetchone() is not None

    def likes(self, ids, viewer=None):
        if not ids:return {}
        placeholders=','.join('?' for _ in ids)
        with self.users._lock:
            viewers=aliases_unlocked(self.users,viewer) or ('',)
            rows=self.users._conn.execute('SELECT l.post_id,COUNT(DISTINCT COALESCE(a.canonical_openid,l.owner)),MAX(CASE WHEN l.owner IN ('+marks(viewers)+') THEN 1 ELSE 0 END) FROM community_likes l LEFT JOIN account_aliases a ON a.alias_openid=l.owner WHERE l.post_id IN ('+placeholders+') GROUP BY l.post_id',(*viewers,*ids)).fetchall()
        return {sid:(count,bool(liked)) for sid,count,liked in rows}

    def set_like(self, sid, owner, liked):
        with self.users._lock, self.users._conn:
            owners=aliases_unlocked(self.users,owner);owner=canonical_unlocked(self.users,owner)
            self.users._conn.execute('DELETE FROM community_likes WHERE post_id=? AND owner IN ('+marks(owners)+')',(sid,*owners))
            if liked:self.users._conn.execute('INSERT OR IGNORE INTO community_likes VALUES(?,?)',(sid,owner))

    def maintenance(self):
        now=time.time()
        with self.users._lock, self.users._conn:
            self.users._conn.execute("UPDATE community_submissions SET status='rejected',reason='投稿处理超时，请重新投稿',updated=? WHERE (status='pending' AND submitted<?) OR (status='uploading' AND submitted<?)",
                                     (now,now-PENDING_TTL,now-3600))
