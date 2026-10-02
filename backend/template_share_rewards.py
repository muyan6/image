"""Exactly-once author rewards, immutable eligibility, and non-confiscating recovery.

This module never awards the consumer. A refund creates recovery metadata rather
than debiting the author's unrelated existing balance; only future author reward
entitlements repay it. All *_unlocked operations belong to the user's existing
SQLite transaction/lock. This is not a second balance authority.
"""
import copy
from datetime import datetime, timedelta, timezone
import json
import time

from account_links import canonical_unlocked, aliases_unlocked, marks, adjust_balance_unlocked


DEFAULT_TEMPLATE_SHARING = {'enabled': True, 'reward_enabled': True,
    'reward_amount': 40, 'reward_mode': 'first_user', 'author_daily_cap': 400,
    'global_daily_cap': 4000}


def validate_policy(policy):
    if not isinstance(policy, dict): raise ValueError('template_sharing 必须是对象')
    for field in ('enabled', 'reward_enabled'):
        if type(policy.get(field)) is not bool: raise ValueError('template_sharing.'+field+' 必须是布尔值')
    if policy.get('reward_mode') not in ('first_user', 'all_success'):
        raise ValueError('template_sharing.reward_mode 必须是 first_user 或 all_success')
    for field, maximum in (('reward_amount',9999),('author_daily_cap',1000000000),('global_daily_cap',1000000000)):
        value = policy.get(field)
        if type(value) is not int or not 0 <= value <= maximum:
            raise ValueError('template_sharing.'+field+' 超出范围')


def reward_snapshot(template, policy):
    """Freeze reviewed template identity and policy before reserving a paid job."""
    candidate = {key: copy.deepcopy(policy.get(key, default)) for key, default in DEFAULT_TEMPLATE_SHARING.items()}
    validate_policy(candidate)
    snapshot = copy.deepcopy(template or {})
    snapshot['share_reward_policy'] = candidate
    return snapshot


SCHEMA = '''
CREATE TABLE IF NOT EXISTS template_reward_jobs(
    job_id TEXT PRIMARY KEY, consumer_openid TEXT NOT NULL, template_id TEXT NOT NULL,
    author_openid TEXT NOT NULL, source_revision TEXT NOT NULL, snapshot TEXT NOT NULL,
    free_mode INTEGER NOT NULL, created_at REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
    reason TEXT NOT NULL DEFAULT '', delivery_confirmed INTEGER NOT NULL DEFAULT 0,
    delivered_at REAL, settled_at REAL);
CREATE INDEX IF NOT EXISTS template_reward_jobs_pending ON template_reward_jobs(status,delivery_confirmed);
CREATE TABLE IF NOT EXISTS template_reward_uses(
    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL UNIQUE,
    consumer_openid TEXT NOT NULL, template_id TEXT NOT NULL, succeeded_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS template_reward_uses_owner ON template_reward_uses(template_id,consumer_openid,id);
CREATE TABLE IF NOT EXISTS template_reward_awards(
    job_id TEXT PRIMARY KEY, author_openid TEXT NOT NULL, consumer_openid TEXT NOT NULL,
    template_id TEXT NOT NULL, source_revision TEXT NOT NULL, amount INTEGER NOT NULL CHECK(amount>=0),
    credited_amount INTEGER NOT NULL CHECK(credited_amount>=0), offset_amount INTEGER NOT NULL CHECK(offset_amount>=0),
    created_at REAL NOT NULL, reversed_at REAL);
CREATE INDEX IF NOT EXISTS template_reward_awards_author ON template_reward_awards(author_openid,created_at);
CREATE INDEX IF NOT EXISTS template_reward_awards_day ON template_reward_awards(created_at);
CREATE TABLE IF NOT EXISTS template_reward_recovery(
    job_id TEXT PRIMARY KEY, author_openid TEXT NOT NULL, amount INTEGER NOT NULL CHECK(amount>=0),
    remaining INTEGER NOT NULL CHECK(remaining>=0), created_at REAL NOT NULL, settled_at REAL);
CREATE INDEX IF NOT EXISTS template_reward_recovery_owner ON template_reward_recovery(author_openid,remaining,created_at);
CREATE TABLE IF NOT EXISTS template_reward_offsets(
    reward_job_id TEXT NOT NULL, recovery_job_id TEXT NOT NULL, amount INTEGER NOT NULL CHECK(amount>0),
    created_at REAL NOT NULL, PRIMARY KEY(reward_job_id,recovery_job_id));
CREATE TABLE IF NOT EXISTS template_reward_ledger(
    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, openid TEXT NOT NULL,
    action TEXT NOT NULL, amount INTEGER NOT NULL, reward_amount INTEGER NOT NULL DEFAULT 0,
    offset_amount INTEGER NOT NULL DEFAULT 0, recovery_amount INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL, UNIQUE(job_id,action));
CREATE INDEX IF NOT EXISTS template_reward_ledger_owner ON template_reward_ledger(openid,created_at,id);
'''


def init_schema_unlocked(db):
    db.executescript(SCHEMA)


def record_eligibility_unlocked(users, job_id, consumer, snapshot, free_mode=False):
    """Admission evidence commits atomically with the actual reservation ledger."""
    if not snapshot or snapshot.get('source') != 'user': return
    template_id = snapshot.get('id'); author = snapshot.get('author_openid'); revision = snapshot.get('source_revision')
    if not all(isinstance(value,str) and 0 < len(value) <= 200 for value in (template_id,author)):
        raise ValueError('已审核模板缺少稳定作者或模板编号')
    if type(revision) is int:
        if revision < 1: raise ValueError('已审核模板版本无效')
    elif not isinstance(revision,str) or not revision.strip() or len(revision)>200:
        raise ValueError('已审核模板版本无效')
    policy = snapshot.get('share_reward_policy')
    validate_policy(policy)
    raw = json.dumps(copy.deepcopy(snapshot),ensure_ascii=False,sort_keys=True,separators=(',',':'))
    users._conn.execute('INSERT INTO template_reward_jobs(job_id,consumer_openid,template_id,author_openid,source_revision,snapshot,free_mode,created_at) '
        'VALUES(?,?,?,?,?,?,?,?)',(job_id,consumer,template_id,author,str(revision),raw,int(bool(free_mode)),time.time()))


def _one(db, query, args):
    cursor = db.execute(query,args); row = cursor.fetchone()
    return dict(zip([field[0] for field in cursor.description],row)) if row else None


def _finish(db, job_id, status, reason, now):
    db.execute('UPDATE template_reward_jobs SET status=?,reason=?,settled_at=? WHERE job_id=?',
               (status,reason,now,job_id))
    return {'status':status,'reason':reason,'reward_amount':0,'credited_amount':0}


def settle_unlocked(users, job_id):
    """Called only at the trusted durable-delivery/charge-completion boundary."""
    db = users._conn; receipt = _one(db,'SELECT * FROM template_reward_jobs WHERE job_id=?',(job_id,))
    if not receipt or receipt['status'] != 'pending': return None
    now = time.time()
    charge = _one(db,'SELECT openid,amount,state,refunded FROM job_charges WHERE job_id=?',(job_id,))
    if not charge or charge['refunded']:
        return _finish(db,job_id,'skipped','missing_or_refunded_charge',now)
    if charge['state'] != 'succeeded' or not receipt['delivery_confirmed']: return None
    if receipt['free_mode'] or type(charge['amount']) is not int or charge['amount'] <= 0:
        return _finish(db,job_id,'skipped','free_or_zero_charge',now)
    consumer = canonical_unlocked(users,charge['openid'])
    if consumer != canonical_unlocked(users,receipt['consumer_openid']):
        return _finish(db,job_id,'skipped','consumer_mismatch',now)
    author = canonical_unlocked(users,receipt['author_openid'])
    if consumer == author: return _finish(db,job_id,'skipped','self_use',now)
    # Record every qualifying successful use, including disabled/capped rewards.
    # Turning a switch or editing/reapproving a template never resets first use.
    db.execute('INSERT OR IGNORE INTO template_reward_uses(job_id,consumer_openid,template_id,succeeded_at) VALUES(?,?,?,?)',
               (job_id,consumer,receipt['template_id'],now))
    policy = json.loads(receipt['snapshot'])['share_reward_policy']; validate_policy(policy)
    if not policy['reward_enabled'] or not policy['reward_amount']:
        return _finish(db,job_id,'skipped','reward_disabled',now)
    consumer_owners = aliases_unlocked(users,consumer)
    if policy['reward_mode'] == 'first_user':
        first = db.execute('SELECT job_id FROM template_reward_uses WHERE template_id=? AND consumer_openid IN ('+marks(consumer_owners)+') '
            'ORDER BY id LIMIT 1',(receipt['template_id'],*consumer_owners)).fetchone()
        if not first or first[0] != job_id: return _finish(db,job_id,'skipped','not_first_user',now)
    person = db.execute('SELECT balance,banned FROM users WHERE openid=?',(author,)).fetchone()
    if not person: return _finish(db,job_id,'skipped','author_missing',now)
    if person[1]: return _finish(db,job_id,'skipped','author_banned',now)
    owners = aliases_unlocked(users,author); clause = ' IN ('+marks(owners)+')'
    midnight = datetime.fromtimestamp(now,timezone(timedelta(hours=8))).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
    amount = policy['reward_amount']
    author_used = db.execute('SELECT COALESCE(SUM(amount),0) FROM template_reward_awards WHERE author_openid'+clause+' AND created_at>=?',
                            (*owners,midnight)).fetchone()[0]
    global_used = db.execute('SELECT COALESCE(SUM(amount),0) FROM template_reward_awards WHERE created_at>=?',(midnight,)).fetchone()[0]
    if author_used + amount > policy['author_daily_cap']:
        return _finish(db,job_id,'skipped','author_daily_cap',now)
    if global_used + amount > policy['global_daily_cap']:
        return _finish(db,job_id,'skipped','global_daily_cap',now)
    recoveries = db.execute('SELECT job_id,remaining FROM template_reward_recovery WHERE author_openid'+clause+
        ' AND remaining>0 ORDER BY created_at,job_id LIMIT ?',(*owners,amount)).fetchall()
    offset = min(amount,sum(row[1] for row in recoveries)); credited = amount-offset
    if type(person[0]) is not int or not -(2**63) <= person[0] < 2**63:
        db.execute("UPDATE template_reward_jobs SET reason='author_balance_invalid' WHERE job_id=?",(job_id,)); return None
    if person[0]+credited >= 2**63:
        db.execute("UPDATE template_reward_jobs SET reason='author_balance_overflow' WHERE job_id=?",(job_id,)); return None
    # A separate payment refund can leave an existing negative balance. Never
    # worsen it or pretend to clear it; reward processing remains pending.
    if person[0] < 0:
        db.execute("UPDATE template_reward_jobs SET reason='author_balance_negative' WHERE job_id=?",(job_id,)); return None
    remaining_offset = offset
    for recovery_id, due in recoveries:
        repaid = min(remaining_offset,due)
        if not repaid: break
        db.execute('UPDATE template_reward_recovery SET remaining=remaining-?,settled_at=CASE WHEN remaining=? THEN ? ELSE settled_at END WHERE job_id=?',
                   (repaid,repaid,now,recovery_id))
        db.execute('INSERT INTO template_reward_offsets VALUES(?,?,?,?)',(job_id,recovery_id,repaid,now))
        remaining_offset -= repaid
    if credited: adjust_balance_unlocked(users,author,credited)
    db.execute('INSERT INTO template_reward_awards VALUES(?,?,?,?,?,?,?,?,?,NULL)',
               (job_id,author,consumer,receipt['template_id'],receipt['source_revision'],amount,credited,offset,now))
    db.execute("INSERT INTO template_reward_ledger(job_id,openid,action,amount,reward_amount,offset_amount,created_at) VALUES(?,?,'credit',?,?,?,?)",
               (job_id,author,credited,amount,offset,now))
    _finish(db,job_id,'awarded','',now)
    return {'status':'awarded','reason':'','reward_amount':amount,'credited_amount':credited,'offset_amount':offset}


def confirm_delivery_unlocked(users, job_id):
    users._conn.execute('UPDATE template_reward_jobs SET delivery_confirmed=1,delivered_at=COALESCE(delivered_at,?) WHERE job_id=?',
                        (time.time(),job_id))
    return settle_unlocked(users,job_id)


def reverse_unlocked(users, job_id):
    """No debit to unrelated existing funds, no negative balance, no lost debt."""
    db = users._conn; now = time.time()
    award = _one(db,'SELECT * FROM template_reward_awards WHERE job_id=?',(job_id,))
    if award and award['reversed_at'] is None:
        author = canonical_unlocked(users,award['author_openid'])
        db.execute('INSERT INTO template_reward_recovery VALUES(?,?,?,?,?,NULL)',(job_id,author,award['amount'],award['amount'],now))
        db.execute("INSERT INTO template_reward_ledger(job_id,openid,action,amount,recovery_amount,created_at) VALUES(?,?,'reversal_pending',0,?,?)",
                   (job_id,author,award['amount'],now))
        db.execute('UPDATE template_reward_awards SET reversed_at=? WHERE job_id=?',(now,job_id))
    db.execute("UPDATE template_reward_jobs SET status='refunded',reason='consumer_refund',settled_at=? WHERE job_id=?",(now,job_id))


def recover_share_rewards(users, statuses=None):
    """Replay immutable receipts after ordinary charge reconciliation, even if
    image retention expired after a previously confirmed successful delivery.
    """
    statuses = statuses or {}
    recovered = 0; cursor = ''
    while True:
        with users._lock:
            pending = users._conn.execute("SELECT r.job_id,r.delivery_confirmed FROM template_reward_jobs r JOIN job_charges c ON c.job_id=r.job_id "
                "WHERE r.status='pending' AND c.state='succeeded' AND c.refunded=0 AND r.job_id>? ORDER BY r.job_id LIMIT 100",(cursor,)).fetchall()
        if not pending: break
        cursor = pending[-1][0]
        for job_id, delivered in pending:
            if not delivered and statuses.get(job_id) != 'succeeded': continue
            if statuses.get(job_id) in ('failed','cancelled_charged'): continue
            with users._lock,users._conn:
                users._conn.execute('BEGIN IMMEDIATE')
                outcome = confirm_delivery_unlocked(users,job_id) if not delivered else settle_unlocked(users,job_id)
                recovered += bool(outcome and outcome['status'] == 'awarded')
    return recovered


def reward_summary(users, openid):
    with users._lock:
        owners = aliases_unlocked(users,openid); clause = ' IN ('+marks(owners)+')'
        gross, credited, offset = users._conn.execute('SELECT COALESCE(SUM(amount),0),COALESCE(SUM(credited_amount),0),COALESCE(SUM(offset_amount),0) '
            'FROM template_reward_awards WHERE author_openid'+clause,owners).fetchone()
        due = users._conn.execute('SELECT COALESCE(SUM(remaining),0) FROM template_reward_recovery WHERE author_openid'+clause,owners).fetchone()[0]
        reversed_points = users._conn.execute('SELECT COALESCE(SUM(amount),0) FROM template_reward_awards WHERE author_openid'+clause+
                                             ' AND reversed_at IS NOT NULL',owners).fetchone()[0]
        return {'reward_earned':gross,'reward_credited':credited,'reward_offset':offset,'recovery_due':due,
                'reward_reversed':reversed_points,'reward_frozen':bool(due),'recovery_policy':'future_author_rewards_only'}
