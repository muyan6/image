"""Account-owned preferences, read-only credit history and durable submission keys.

All writes share the user's SQLite lock/connection. Submission keys are recorded
before admission, with a fixed job id, so a lost response never opens a new debit.
"""
import hashlib
import json
import re
import time
import uuid


class ExperienceStore:
    def __init__(self, users):
        self.users = users
        with users._lock, users._conn:
            users._conn.executescript('''
                CREATE TABLE IF NOT EXISTS template_preferences(
                    openid TEXT NOT NULL, template_id TEXT NOT NULL,
                    favorite INTEGER NOT NULL DEFAULT 0, used_at REAL,
                    updated_at REAL NOT NULL, PRIMARY KEY(openid,template_id));
                CREATE TABLE IF NOT EXISTS client_submissions(
                    openid TEXT NOT NULL, request_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, job_id TEXT NOT NULL,
                    state TEXT NOT NULL, input_mode TEXT NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    error TEXT NOT NULL DEFAULT '', http_status INTEGER,
                    PRIMARY KEY(openid,request_id), UNIQUE(job_id));
                CREATE INDEX IF NOT EXISTS client_submission_user_time
                    ON client_submissions(openid,created_at DESC);
            ''')
            # An earlier process may have died between the two DB commits. Do not
            # declare this unaccepted or replay it; the fixed id can find its job.
            users._conn.execute("UPDATE client_submissions SET state='uncertain' WHERE state='pending'")

    def preferences(self, openid, available=None):
        with self.users._lock:
            rows = self.users._conn.execute(
                'SELECT template_id,favorite,used_at,updated_at FROM template_preferences '
                'WHERE openid=? ORDER BY updated_at DESC,template_id', (openid,)).fetchall()
        if available is not None:
            rows = [r for r in rows if r[0] in available]
        return {'template_favorites': [r[0] for r in rows if r[1]],
                'recent_templates': [r[0] for r in sorted(rows, key=lambda r: r[2] or 0, reverse=True)
                                     if r[2] is not None][:20]}

    def preference(self, openid, template_id, favorite=None, recent=False):
        now = time.time()
        with self.users._lock, self.users._conn:
            self.users._conn.execute('BEGIN IMMEDIATE')
            self.users._conn.execute(
                'INSERT OR IGNORE INTO template_preferences(openid,template_id,updated_at) VALUES(?,?,?)',
                (openid, template_id, now))
            if favorite is not None:
                self.users._conn.execute(
                    'UPDATE template_preferences SET favorite=?,updated_at=? WHERE openid=? AND template_id=?',
                    (int(favorite), now, openid, template_id))
            if recent:
                self.users._conn.execute(
                    'UPDATE template_preferences SET used_at=?,updated_at=? WHERE openid=? AND template_id=?',
                    (now, now, openid, template_id))
                # Retain only the last 20 recent ids; favorites remain intact.
                self.users._conn.execute(
                    'UPDATE template_preferences SET used_at=NULL WHERE openid=? AND used_at IS NOT NULL '
                    'AND template_id NOT IN (SELECT template_id FROM template_preferences WHERE openid=? '
                    'AND used_at IS NOT NULL ORDER BY used_at DESC,template_id LIMIT 20)', (openid, openid))

    def _submission(self, openid, request_id):
        cur = self.users._conn.execute(
            'SELECT * FROM client_submissions WHERE openid=? AND request_id=?', (openid, request_id))
        row = cur.fetchone()
        return dict(zip([c[0] for c in cur.description], row)) if row else None

    def submission(self, openid, request_id):
        with self.users._lock:
            return self._submission(openid, request_id)

    def claim(self, openid, request_id, payload, input_mode):
        if not re.fullmatch(r'[A-Za-z0-9_-]{8,80}', request_id or ''):
            raise ValueError('提交编号格式无效')
        raw = json.dumps({'input_mode': input_mode, 'payload': payload}, ensure_ascii=False,
                         sort_keys=True, separators=(',', ':'))
        fingerprint = hashlib.sha256(raw.encode('utf-8')).hexdigest()
        now = time.time()
        with self.users._lock, self.users._conn:
            self.users._conn.execute('BEGIN IMMEDIATE')
            row = self._submission(openid, request_id)
            if row:
                if row['fingerprint'] != fingerprint:
                    raise ValueError('同一提交编号的参数不一致，请先确认原任务')
                return row, False
            if self.users._purging:
                raise ValueError('账号清理中，请稍后再试')
            self.users._conn.execute(
                'INSERT INTO client_submissions(openid,request_id,fingerprint,job_id,state,input_mode,created_at,updated_at) '
                'VALUES(?,?,?,?,?,?,?,?)',
                (openid, request_id, fingerprint, uuid.uuid4().hex[:12], 'pending', input_mode, now, now))
            return self._submission(openid, request_id), True

    def settle(self, openid, request_id, state, error='', http_status=None):
        with self.users._lock, self.users._conn:
            self.users._conn.execute(
                'UPDATE client_submissions SET state=?,error=?,http_status=?,updated_at=? '
                'WHERE openid=? AND request_id=?',
                (state, str(error)[:500], http_status, time.time(), openid, request_id))

    def settlement(self, openid, job_id):
        with self.users._lock:
            row = self.users._conn.execute(
                'SELECT amount,refunded,state FROM job_charges WHERE openid=? AND job_id=?',
                (openid, job_id)).fetchone()
        if not row:
            return {'charged_amount': None, 'refunded_amount': None, 'settlement': 'historical_unknown'}
        return {'charged_amount': int(row[0]), 'refunded_amount': int(row[0]) if row[1] else 0,
                'settlement': row[2]}

    def credit_records(self, openid, offset=0, limit=30):
        """Expose recorded amounts only; never infer an old welcome balance or delta.

        Debits are sourced from the reservation ledger, refunds/rewards from
        audited amounts, payment entries from their ledger. No mutation or balance
        reconstruction occurs here. Unknown absolute admin edits are omitted.
        """
        offset = max(0, offset); limit = max(1, min(100, limit))
        records = []
        def add(identity, kind, title, amount, at, job_id=None, order_id=None):
            if type(amount) is int and amount:
                records.append({'id': identity, 'kind': kind, 'title': title, 'amount': amount,
                                'created_at': at, 'job_id': job_id, 'order_id': order_id})
        with self.users._lock:
            db = self.users._conn
            for jid, amount, at in db.execute(
                    'SELECT job_id,amount,created_at FROM job_charges WHERE openid=?', (openid,)):
                add('charge:' + jid, 'generation', '生成作品', -int(amount), at, jid)
            violations = {r[0]: int(r[1]) for r in db.execute(
                'SELECT id,charged FROM violations WHERE openid=?', (openid,))}
            for aid, at, action, detail in db.execute(
                    "SELECT id,ts,action,detail FROM audit WHERE openid=? AND action IN "
                    "('refund','earn_checkin','earn_video','community_featured','blocked','violation_review','admin_adjust_balance')",
                    (openid,)):
                amount = None; title = ''; kind = action; jid = None
                job = re.search(r'(?:^|\s)job=([A-Za-z0-9_-]+)', detail)
                if job: jid = job.group(1)
                plus = re.search(r'(?:^|\s)\+=(\d+)|(?:^|\s)\+(\d+)', detail)
                if action == 'refund' and jid and plus:
                    amount = int(plus.group(1) or plus.group(2)); title = '生成退款'
                elif action in ('earn_checkin', 'earn_video', 'community_featured') and plus:
                    amount = int(plus.group(1) or plus.group(2))
                    title = {'earn_checkin': '签到奖励', 'earn_video': '视频奖励',
                             'community_featured': '社区精选奖励'}[action]
                elif action == 'blocked':
                    charged = re.search(r'(?:^|\s)charged=(\d+)', detail)
                    if charged: amount = -int(charged.group(1)); title = '审核扣除'
                elif action == 'violation_review' and 'status=overturned' in detail:
                    identity = re.search(r'(?:^|\s)id=([A-Za-z0-9_-]+)', detail)
                    if identity and identity.group(1) in violations:
                        amount = violations[identity.group(1)]; title = '审核申诉退款'
                elif action == 'admin_adjust_balance':
                    delta = re.search(r'(?:^|\s)delta=(-?\d+)(?:\s|$)', detail)
                    if delta: amount = int(delta.group(1)); title = '光子调整'
                if amount is not None: add('audit:' + str(aid), kind, title, amount, at, jid)
            # The bindings retain historical reward amounts; older NULL entries
            # explicitly lack that evidence and are not replaced by today's price.
            for invitee, inviter, at, reward in db.execute(
                    'SELECT invitee,inviter,created_at,reward FROM invite_bindings WHERE invitee=? OR inviter=?',
                    (openid, openid)):
                if reward is not None:
                    identity = hashlib.sha256((openid + '\0' + invitee).encode()).hexdigest()[:20]
                    add('invite:' + identity, 'invite', '邀请奖励', int(reward), at)
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='payment_ledger'").fetchone()
            if exists:
                for lid, oid, action, delta, at in db.execute(
                        'SELECT id,order_id,action,delta,created_at FROM payment_ledger WHERE openid=?', (openid,)):
                    add('payment:' + str(lid), 'payment_' + action,
                        '充值到账' if action == 'credit' else '充值退款扣回', int(delta), at, order_id=oid)
        records.sort(key=lambda r: (r['created_at'], r['id']), reverse=True)
        total = len(records); items = records[offset:offset + limit]
        return {'items': items, 'total': total, 'has_more': offset + len(items) < total,
                'next_offset': offset + len(items), 'history_complete': False,
                'history_note': '仅展示已记录金额的流水，历史赠送及未记录增减金额不作推算'}


def store_for(users):
    # UserStore replacement in isolated tests/deployments must not retain another
    # account DB. Keep the cache on the actual store, not a module global.
    with users._lock:
        existing = getattr(users, '_experience_store', None)
    if existing is not None:
        return existing
    # Serialize initialization without recursively taking the connection lock.
    with _init_lock:
        if getattr(users, '_experience_store', None) is None:
            users._experience_store = ExperienceStore(users)
        return users._experience_store


import threading
_init_lock = threading.Lock()
