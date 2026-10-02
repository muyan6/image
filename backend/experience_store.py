"""Account-owned preferences, read-only credit history and durable submission keys.

All writes share the user's SQLite lock/connection. Submission keys are recorded
before admission, with a fixed job id, so a lost response never opens a new debit.
"""
import hashlib
import json
import re
import time
import uuid
from account_links import canonical_unlocked, aliases_unlocked, marks
from read_projection import CreditProjection


class ExperienceStore:
    def __init__(self, users):
        self.users = users
        self._credit_projection = CreditProjection(users)
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
            owners = aliases_unlocked(self.users, openid)
            rows = self.users._conn.execute(
                'SELECT template_id,favorite,used_at,updated_at FROM template_preferences '
                'WHERE openid IN ('+marks(owners)+') ORDER BY updated_at DESC,template_id', owners).fetchall()
        combined = {}
        for identity, favorite, used_at, updated_at in rows:
            previous = combined.get(identity, (identity, 0, None, 0))
            combined[identity] = (identity, max(favorite, previous[1]), max(used_at or 0, previous[2] or 0) or None, max(updated_at, previous[3]))
        rows = sorted(combined.values(), key=lambda row: (-row[3], row[0]))
        if available is not None:
            rows = [r for r in rows if r[0] in available]
        return {'template_favorites': [r[0] for r in rows if r[1]],
                'recent_templates': [r[0] for r in sorted(rows, key=lambda r: r[2] or 0, reverse=True)
                                     if r[2] is not None][:20]}

    def preference(self, openid, template_id, favorite=None, recent=False):
        now = time.time()
        with self.users._lock, self.users._conn:
            self.users._conn.execute('BEGIN IMMEDIATE')
            owners = aliases_unlocked(self.users, openid)
            openid = canonical_unlocked(self.users, openid)
            self.users._conn.execute(
                'INSERT OR IGNORE INTO template_preferences(openid,template_id,updated_at) VALUES(?,?,?)',
                (openid, template_id, now))
            if favorite is not None:
                self.users._conn.execute(
                    'UPDATE template_preferences SET favorite=?,updated_at=? WHERE openid IN ('+marks(owners)+') AND template_id=?',
                    (int(favorite), now, *owners, template_id))
            if recent:
                self.users._conn.execute(
                    'UPDATE template_preferences SET used_at=?,updated_at=? WHERE openid=? AND template_id=?',
                    (now, now, openid, template_id))
                # Retain only the last 20 recent ids; favorites remain intact.
                self.users._conn.execute(
                    'UPDATE template_preferences SET used_at=NULL WHERE openid IN ('+marks(owners)+') AND used_at IS NOT NULL '
                    'AND template_id NOT IN (SELECT template_id FROM template_preferences WHERE openid IN ('+marks(owners)+') '
                    'AND used_at IS NOT NULL GROUP BY template_id ORDER BY MAX(used_at) DESC,template_id LIMIT 20)', (*owners, *owners))

    def _submission(self, openid, request_id):
        owners = aliases_unlocked(self.users, openid)
        cur = self.users._conn.execute(
            'SELECT * FROM client_submissions WHERE openid IN ('+marks(owners)+') AND request_id=?', (*owners, request_id))
        rows = cur.fetchall()
        if len(rows) > 1:
            raise ValueError('绑定账户的提交编号存在冲突，请在作品页核对')
        row = rows[0] if rows else None
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
            openid = canonical_unlocked(self.users, openid)
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
            row = self._submission(openid, request_id)
            if not row:
                return
            self.users._conn.execute(
                'UPDATE client_submissions SET state=?,error=?,http_status=?,updated_at=? '
                'WHERE openid=? AND request_id=?',
                (state, str(error)[:500], http_status, time.time(), row['openid'], request_id))

    def settlements(self, openid, job_ids):
        """One owner-checked ledger query for a page; unknown history stays unknown."""
        ids = tuple(dict.fromkeys(job_ids))
        unknown = {'charged_amount': None, 'refunded_amount': None, 'settlement': 'historical_unknown'}
        result = {jid: dict(unknown) for jid in ids}
        if not ids:
            return result
        with self.users._lock:
            owners = aliases_unlocked(self.users, openid)
            rows = self.users._conn.execute(
                'SELECT job_id,amount,refunded,state FROM job_charges WHERE openid IN ('+marks(owners)+') '
                'AND job_id IN ('+marks(ids)+')', (*owners, *ids)).fetchall()
        for jid, amount, refunded, state in rows:
            result[jid] = {'charged_amount': int(amount), 'refunded_amount': int(amount) if refunded else 0,
                           'settlement': state}
        return result

    def settlement(self, openid, job_id):
        return self.settlements(openid, (job_id,))[job_id]

    def credit_record_count(self, openid):
        """Exact recorded-history count, with bounded revision-aware metadata cache."""
        return self._credit_projection.count(openid)

    def credit_records(self, openid, offset=0, limit=30):
        """Read-only SQLite pagination; recorded amounts never reconstruct balance."""
        offset = max(0, offset); limit = max(1, min(100, limit))
        items, total = self._credit_projection.page(openid, offset, limit)
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
