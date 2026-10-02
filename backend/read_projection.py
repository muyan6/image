"""Bounded, read-only SQLite projections of recorded credit history.

No balance is inferred or cached here. SQLite does ordering/pagination; Python
only materializes one page. Historical audit parsing runs on a separate read
snapshot outside the account write lock whenever committed data is available.
"""
from collections import OrderedDict
from contextlib import contextmanager
import hashlib
from pathlib import Path
import re
import sqlite3
import threading
from types import SimpleNamespace

from account_links import aliases_unlocked, marks


def parse_audit(action, detail, reviewed_charge=None):
    """The original recorded-amount rules, including unknown-history omissions."""
    amount = None; title = ''; jid = None
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
        if reviewed_charge is not None:
            amount = int(reviewed_charge); title = '审核申诉退款'
    elif action == 'admin_adjust_balance':
        delta = re.search(r'(?:^|\s)delta=(-?\d+)(?:\s|$)', detail)
        if delta: amount = int(delta.group(1)); title = '光子调整'
    return (str(amount) if amount else None, title, jid)


class CreditProjection:
    COUNT_CACHE_SIZE = 128
    AUDIT_CACHE_SIZE = 256

    def __init__(self, users):
        self.users = users
        self._counts = OrderedDict()
        self._cache_lock = threading.Lock()

    def _revision_unlocked(self):
        db = self.users._conn
        # data_version changes for other connections; total_changes changes for
        # this connection (also after rolled-back writes, harmless invalidation).
        return (id(db), db.total_changes, db.execute('PRAGMA data_version').fetchone()[0])

    def _revision(self):
        with self.users._lock:
            return self._revision_unlocked()

    def _cached_count(self, key):
        with self._cache_lock:
            value = self._counts.get(key)
            if value is not None: self._counts.move_to_end(key)
            return value

    def _remember_count(self, key, value):
        with self._cache_lock:
            self._counts[key] = value
            self._counts.move_to_end(key)
            while len(self._counts) > self.COUNT_CACHE_SIZE:
                self._counts.popitem(last=False)

    @staticmethod
    def _register(db):
        # Cache only small parsed values, never raw audit detail or whole history.
        parsed = OrderedDict()
        def audit_part(action, detail, reviewed_charge, part):
            digest = hashlib.sha256(detail.encode('utf-8')).digest()
            key = (action, digest, reviewed_charge)
            value = parsed.get(key)
            if value is None:
                value = parse_audit(action, detail, reviewed_charge)
                if sum(len(part or '') for part in value) <= 4096:
                    parsed[key] = value
                    if len(parsed) > CreditProjection.AUDIT_CACHE_SIZE: parsed.popitem(last=False)
            else: parsed.move_to_end(key)
            return value[part]
        db.create_function('credit_audit_amount', 3, lambda a, d, c: audit_part(a, d, c, 0), deterministic=True)
        db.create_function('credit_audit_title', 3, lambda a, d, c: audit_part(a, d, c, 1), deterministic=True)
        db.create_function('credit_audit_job', 3, lambda a, d, c: audit_part(a, d, c, 2), deterministic=True)
        def review_id(detail):
            match = re.search(r'(?:^|\s)id=([A-Za-z0-9_-]+)', detail)
            return match.group(1) if match else None
        db.create_function('credit_review_id', 1, review_id, deterministic=True)
        db.create_function('credit_invite_id', 2,
            lambda owner, invitee: 'invite:' + hashlib.sha256((owner + '\0' + invitee).encode()).hexdigest()[:20],
            deterministic=True)
        def amount_text(value, multiplier):
            amount = int(value) * multiplier
            return str(amount) if amount else None
        db.create_function('credit_amount', 2, amount_text, deterministic=True)

    @contextmanager
    def _snapshot(self):
        with self.users._lock:
            writer = self.users._conn
            revision = self._revision_unlocked()
            path = getattr(self.users, '_path', None)
            if writer.in_transaction or not path:
                # Preserve reads of an existing uncommitted transaction (also
                # useful for in-memory fixtures). Never commit/rollback it here.
                self._register(writer)
                yield writer, revision, False
                return
        reader = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
        try:
            reader.execute('PRAGMA query_only=ON')
            reader.execute('PRAGMA cache_size=-1024')
            reader.execute('PRAGMA temp_store=FILE')
            self._register(reader)
            reader.execute('BEGIN')
            yield reader, revision, True
        finally:
            reader.close()

    @staticmethod
    def _query(db, openid):
        owners = aliases_unlocked(SimpleNamespace(_conn=db), openid)
        clause = ' IN (' + marks(owners) + ')'
        # Text amounts preserve arbitrary recorded Python integers in historical
        # audit details; SQLite must not round them through REAL/json numbers.
        query = '''SELECT 'charge:' || job_id AS id, 'generation' AS kind,
            '生成作品' AS title, credit_amount(amount,-1) AS amount,
            created_at, job_id, NULL AS order_id FROM job_charges WHERE openid''' + clause + ''' AND amount!=0
            UNION ALL SELECT 'audit:' || a.id, a.action,
            credit_audit_title(a.action,a.detail,v.charged),
            credit_audit_amount(a.action,a.detail,v.charged),a.ts,
            credit_audit_job(a.action,a.detail,v.charged),NULL
            FROM audit a LEFT JOIN violations v ON a.action='violation_review'
            AND v.id=credit_review_id(a.detail) AND v.openid''' + clause + '''
            WHERE a.openid''' + clause + ''' AND a.action IN
            ('refund','earn_checkin','earn_video','community_featured','blocked','violation_review','admin_adjust_balance')
            UNION ALL SELECT credit_invite_id(?,invitee),'invite','邀请奖励',
            credit_amount(reward,1),created_at,NULL,NULL FROM invite_bindings
            WHERE (invitee''' + clause + ' OR inviter' + clause + ''') AND reward IS NOT NULL AND reward!=0'''
        params = (*owners, *owners, *owners, openid, *owners, *owners)
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='payment_ledger'").fetchone():
            query += ''' UNION ALL SELECT 'payment:' || id,'payment_' || action,
                CASE WHEN action='credit' THEN '充值到账' ELSE '充值退款扣回' END,
                credit_amount(delta,1),created_at,NULL,order_id FROM payment_ledger WHERE openid''' + clause + ' AND delta!=0'
            params += owners
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='template_reward_ledger'").fetchone():
            # A pending reversal/offset has a real zero balance delta, not an
            # invented debit. Retain that row with its recovery metadata.
            query += ''' UNION ALL SELECT 'template_reward:' || id,'template_reward_' || action,
                CASE WHEN action='reversal_pending' THEN '模板奖励退款待追回（仅抵扣未来作者奖励）'
                     WHEN offset_amount>0 THEN '模板作者奖励（抵扣待追回光子）' ELSE '模板作者奖励' END,
                CAST(amount AS TEXT),created_at,job_id,NULL FROM template_reward_ledger WHERE openid''' + clause
            params += owners
        return query, params, owners

    @staticmethod
    def _count(db, query, params):
        return db.execute('SELECT COUNT(*) FROM (' + query + ') WHERE amount IS NOT NULL', params).fetchone()[0]

    def count(self, openid):
        # Cache hit touches metadata/aliases only, never history rows or regex.
        with self.users._lock:
            revision = self._revision_unlocked()
            owners = aliases_unlocked(self.users, openid)
            key = (openid, owners, revision)
            cached = None if self.users._conn.in_transaction else self._cached_count(key)
        if cached is not None: return cached
        with self._snapshot() as (db, revision, detached):
            query, params, owners = self._query(db, openid)
            total = self._count(db, query, params)
            unchanged = self._revision() == revision if detached else False
            if unchanged: self._remember_count((openid, owners, revision), total)
            return total

    def page(self, openid, offset, limit):
        with self._snapshot() as (db, revision, detached):
            query, params, owners = self._query(db, openid)
            key = (openid, owners, revision)
            cached = self._cached_count(key) if detached else None
            cursor = db.execute('SELECT * FROM (' + query + ') WHERE amount IS NOT NULL '
                'ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?', (*params, limit, offset))
            columns = [field[0] for field in cursor.description]
            items = [dict(zip(columns, row)) for row in cursor.fetchall()]
            for row in items: row['amount'] = int(row['amount'])
            reward_ids = [int(row['id'].split(':')[1]) for row in items if row['id'].startswith('template_reward:')]
            if reward_ids:
                rows = db.execute('SELECT id,reward_amount,offset_amount,recovery_amount FROM template_reward_ledger '
                    'WHERE openid IN ('+marks(owners)+') AND id IN ('+marks(reward_ids)+')',(*owners,*reward_ids)).fetchall()
                metadata = {str(identity):{'reward_amount':gross,'offset_amount':offset,'recovery_amount':recovery}
                            for identity,gross,offset,recovery in rows}
                for row in items:
                    if row['id'].startswith('template_reward:'): row.update(metadata[row['id'].split(':')[1]])
            unchanged = self._revision() == revision if detached else False
            total = cached if cached is not None and unchanged else self._count(db, query, params)
            if unchanged: self._remember_count(key, total)
            return items, total
