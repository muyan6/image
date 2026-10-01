"""Bounded, persistent comments and reports. Only WeChat-pass comments are public."""
import threading
import time
import uuid

_factory_lock = threading.Lock()
REPORT_REASONS = {'spam': '广告引流', 'abuse': '辱骂攻击', 'inappropriate': '不适宜内容',
                  'privacy': '侵犯隐私', 'copyright': '侵权', 'other': '其他问题'}


def interactions_for(users):
    with _factory_lock:
        if not hasattr(users, '_community_interactions'):
            users._community_interactions = InteractionStore(users)
        return users._community_interactions


class InteractionStore:
    def __init__(self, users):
        self.users = users
        with users._lock, users._conn:
            users._conn.executescript('''
                CREATE TABLE IF NOT EXISTS community_comments(
                    id TEXT PRIMARY KEY, post_id TEXT NOT NULL, owner TEXT NOT NULL,
                    request_id TEXT NOT NULL, content TEXT NOT NULL, status TEXT NOT NULL,
                    created REAL NOT NULL, updated REAL NOT NULL, verdict TEXT NOT NULL DEFAULT '',
                    UNIQUE(owner,request_id));
                CREATE INDEX IF NOT EXISTS community_comment_post ON community_comments(post_id,status,created);
                CREATE TABLE IF NOT EXISTS community_comment_likes(
                    comment_id TEXT, owner TEXT, PRIMARY KEY(comment_id,owner));
                CREATE TABLE IF NOT EXISTS community_reports(
                    id TEXT PRIMARY KEY, target_type TEXT NOT NULL, target_id TEXT NOT NULL,
                    post_id TEXT NOT NULL, owner TEXT NOT NULL, reason TEXT NOT NULL,
                    snapshot TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', created REAL NOT NULL,
                    UNIQUE(target_type,target_id,owner));
                CREATE INDEX IF NOT EXISTS community_report_status ON community_reports(status,created);
                CREATE TABLE IF NOT EXISTS community_interaction_events(owner TEXT,kind TEXT,at REAL);
                CREATE INDEX IF NOT EXISTS community_interaction_rate ON community_interaction_events(owner,kind,at);
            ''')

    def _get(self, cid):
        cur = self.users._conn.execute('SELECT * FROM community_comments WHERE id=?', (cid,))
        row = cur.fetchone()
        return dict(zip([c[0] for c in cur.description], row)) if row else None

    def get(self, cid):
        with self.users._lock:
            return self._get(cid)

    def _rate(self, owner, kind, per_minute, per_hour):
        db = self.users._conn; now = time.time()
        db.execute('DELETE FROM community_interaction_events WHERE at<?', (now-3600,))
        minute, hour = db.execute('SELECT COALESCE(SUM(at>?),0),COUNT(*) FROM community_interaction_events WHERE owner=? AND kind=?',
                                  (now-60, owner, kind)).fetchone()
        if minute >= per_minute or hour >= per_hour:
            raise ValueError('操作较频繁，请稍后再试')
        db.execute('INSERT INTO community_interaction_events VALUES(?,?,?)', (owner,kind,now))

    def reserve_comment(self, post_id, owner, request_id, content):
        now = time.time()
        with self.users._lock, self.users._conn:
            db = self.users._conn; db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE community_comments SET status='failed',content='' WHERE status='reviewing' AND updated<?",(now-120,))
            old = db.execute('SELECT id FROM community_comments WHERE owner=? AND request_id=?', (owner,request_id)).fetchone()
            if old:
                row = self._get(old[0])
                if row['post_id'] != post_id or (row['content'] and row['content'] != content):
                    raise ValueError('提交标识已使用，请重新提交')
                if row['status'] == 'published': return row, False
                if row['status'] == 'reviewing' and row['updated'] > now-120:
                    raise ValueError('评论正在审核，请稍后核对')
                if row['status'] not in ('failed','reviewing'):
                    raise ValueError('此评论未通过审核或已删除，请重新提交')
            self._rate(owner,'comment',5,30)
            count = db.execute("SELECT COUNT(*) FROM community_comments WHERE post_id=? AND status IN ('published','reviewing')",(post_id,)).fetchone()[0]
            if count >= 1000: raise ValueError('此作品的留言区已满')
            cid = old[0] if old else 'c_'+uuid.uuid4().hex
            if old:
                db.execute("UPDATE community_comments SET status='reviewing',content=?,updated=? WHERE id=?",(content,now,cid))
            else:
                db.execute("INSERT INTO community_comments(id,post_id,owner,request_id,content,status,created,updated) VALUES(?,?,?,?,?,'reviewing',?,?)",
                           (cid,post_id,owner,request_id,content,now,now))
            return self._get(cid), True

    def finish_comment(self, cid, status, verdict=''):
        with self.users._lock, self.users._conn:
            self.users._conn.execute("UPDATE community_comments SET status=?,verdict=?,updated=?,content=CASE WHEN ?='published' THEN content ELSE '' END WHERE id=? AND status='reviewing'",
                                     (status,verdict[:100],time.time(),status,cid))
            return self._get(cid)

    def delete_comment(self, cid, owner=None):
        with self.users._lock, self.users._conn:
            row = self._get(cid)
            if not row or (owner is not None and row['owner'] != owner): raise KeyError('评论不存在')
            self.users._conn.execute("UPDATE community_comments SET status='deleted',content='',updated=? WHERE id=?",(time.time(),cid))
            self.users._conn.execute('DELETE FROM community_comment_likes WHERE comment_id=?',(cid,))

    def comments(self, *, post_id=None, viewer=None, offset=0, limit=30, admin=False):
        where = " WHERE c.status='published'" if admin else " WHERE c.status='published' AND u.banned=0"
        args = []
        if post_id is not None: where += ' AND c.post_id=?'; args.append(post_id)
        join = ' FROM community_comments c LEFT JOIN users u ON u.openid=c.owner'
        offset=max(0,offset);limit=max(1,min(50,limit))
        with self.users._lock:
            total=self.users._conn.execute('SELECT COUNT(*)'+join+where,args).fetchone()[0]
            rows=self.users._conn.execute('SELECT c.id,c.post_id,c.content,c.created,u.nickname,c.owner'+join+where+
                                         ' ORDER BY c.created DESC,c.id LIMIT ? OFFSET ?',(*args,limit,offset)).fetchall()
            ids=[r[0] for r in rows]; likes={}
            if ids:
                like_rows=self.users._conn.execute('SELECT comment_id,COUNT(*),MAX(owner=?) FROM community_comment_likes WHERE comment_id IN ('+','.join('?' for _ in ids)+') GROUP BY comment_id',(viewer or '',*ids)).fetchall()
                likes={r[0]:(r[1],bool(r[2])) for r in like_rows}
        items=[{'id':r[0],'post_id':r[1],'content':r[2],'created_at':r[3],
                'author_name':r[4] or '新生创作者','mine':r[5]==viewer,
                'likes':likes.get(r[0],(0,False))[0],'liked':likes.get(r[0],(0,False))[1]} for r in rows]
        return {'items':items,'total':total,'next_offset':offset+len(items),'has_more':offset+len(items)<total}

    def counts(self, ids):
        if not ids:return {}
        with self.users._lock:
            rows=self.users._conn.execute("SELECT c.post_id,COUNT(*) FROM community_comments c JOIN users u ON u.openid=c.owner WHERE c.status='published' AND u.banned=0 AND c.post_id IN ("+','.join('?' for _ in ids)+') GROUP BY c.post_id',ids).fetchall()
        return dict(rows)

    def like(self, cid, owner, liked):
        with self.users._lock, self.users._conn:
            row=self._get(cid)
            if not row or row['status']!='published':raise KeyError('评论不存在')
            if liked:self.users._conn.execute('INSERT OR IGNORE INTO community_comment_likes VALUES(?,?)',(cid,owner))
            else:self.users._conn.execute('DELETE FROM community_comment_likes WHERE comment_id=? AND owner=?',(cid,owner))
            count,state=self.users._conn.execute('SELECT COUNT(*),COALESCE(MAX(owner=?),0) FROM community_comment_likes WHERE comment_id=?',(owner,cid)).fetchone()
            return {'id':cid,'likes':count,'liked':bool(state)}

    def report(self, target_type, target_id, post_id, owner, reason, snapshot):
        with self.users._lock, self.users._conn:
            db=self.users._conn;db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT id FROM community_reports WHERE target_type=? AND target_id=? AND owner=?',(target_type,target_id,owner)).fetchone()
            if old:return {'ok':True,'id':old[0],'duplicate':True}
            self._rate(owner,'report',3,20)
            rid='r_'+uuid.uuid4().hex
            db.execute('INSERT INTO community_reports(id,target_type,target_id,post_id,owner,reason,snapshot,created) VALUES(?,?,?,?,?,?,?,?)',
                       (rid,target_type,target_id,post_id,owner,reason,snapshot[:1000],time.time()))
            return {'ok':True,'id':rid,'duplicate':False}

    def reports(self, status='open', offset=0, limit=30):
        if status not in ('open','resolved','all'):raise ValueError('举报状态无效')
        where='' if status=='all' else ' WHERE status=?';args=[] if status=='all' else [status]
        offset=max(0,offset);limit=max(1,min(50,limit))
        with self.users._lock:
            total=self.users._conn.execute('SELECT COUNT(*) FROM community_reports'+where,args).fetchone()[0]
            cur=self.users._conn.execute('SELECT id,target_type,target_id,post_id,reason,snapshot,status,created FROM community_reports'+where+' ORDER BY created DESC,id LIMIT ? OFFSET ?',(*args,limit,offset))
            cols=[c[0] for c in cur.description];items=[dict(zip(cols,r)) for r in cur.fetchall()]
        return {'items':items,'total':total,'has_more':offset+len(items)<total,'next_offset':offset+len(items)}

    def resolve_report(self, rid):
        with self.users._lock, self.users._conn:
            if not self.users._conn.execute("UPDATE community_reports SET status='resolved' WHERE id=?",(rid,)).rowcount:
                raise KeyError('举报不存在')
