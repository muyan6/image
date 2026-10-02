"""Verified account links: one shared balance, aliases for immutable history owners.

The unlocked helpers are read-only and are safe inside UserStore's non-reentrant
lock. Binding changes only users.db in one transaction; jobs/objects keep their
original owners and callers authorize them through the complete alias set.
"""
import time


SCHEMA = '''
CREATE TABLE IF NOT EXISTS account_aliases(
  alias_openid TEXT PRIMARY KEY, canonical_openid TEXT NOT NULL UNIQUE,
  linked_at REAL NOT NULL, unionid TEXT NOT NULL, evidence_app_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS verified_wechat_identities(
  app_id TEXT NOT NULL, openid TEXT NOT NULL, unionid TEXT NOT NULL,
  kind TEXT NOT NULL, verified_at REAL NOT NULL, PRIMARY KEY(app_id,openid));
CREATE INDEX IF NOT EXISTS verified_wechat_union
  ON verified_wechat_identities(unionid,kind,app_id);
'''


def init_links(users):
    with users._lock, users._conn:
        users._conn.executescript(SCHEMA)


def _has_table(users, name):
    return users._conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def canonical_unlocked(users, identity):
    if not identity or not _has_table(users, 'account_aliases'):
        return identity
    row = users._conn.execute('SELECT canonical_openid FROM account_aliases WHERE alias_openid=?', (identity,)).fetchone()
    return row[0] if row else identity


def aliases_unlocked(users, identity):
    owner = canonical_unlocked(users, identity)
    if not owner or not _has_table(users, 'account_aliases'):
        return (owner,) if owner else ()
    rows = users._conn.execute('SELECT alias_openid FROM account_aliases WHERE canonical_openid=? ORDER BY alias_openid', (owner,)).fetchall()
    return (owner, *(row[0] for row in rows))


def canonical(users, identity):
    with users._lock:
        return canonical_unlocked(users, identity)


def aliases(users, identity):
    with users._lock:
        return aliases_unlocked(users, identity)


def owns(users, stored_owner, current_owner):
    with users._lock:
        return bool(stored_owner and current_owner and canonical_unlocked(users, stored_owner) == canonical_unlocked(users, current_owner))


def marks(values):
    return ','.join('?' for _ in values)


def adjust_balance_unlocked(users, identity, delta):
    """Exact signed-64-bit accounting inside the caller's lock and transaction."""
    if type(delta) is not int:
        raise ValueError('光子增减量必须为整数')
    owner=canonical_unlocked(users,identity)
    row=users._conn.execute('SELECT balance FROM users WHERE openid=?',(owner,)).fetchone()
    if not row or type(row[0]) is not int:
        raise ValueError('账户余额字段异常，请由后台核对')
    result=row[0]+delta
    if not -(2**63) <= result < 2**63:
        raise ValueError('光子余额超出数据库整数范围，请由后台核对')
    users._conn.execute('UPDATE users SET balance=? WHERE openid=?',(result,owner))
    return result


def register_verified(users, app_id, openid, unionid, kind='mini'):
    if kind not in ('mini', 'official', 'open') or not all(isinstance(x, str) and x and len(x) <= 200 for x in (app_id, openid, unionid)):
        raise ValueError('微信身份核验资料不完整')
    init_links(users)
    with users._lock, users._conn:
        users._conn.execute('BEGIN IMMEDIATE')
        old = users._conn.execute('SELECT unionid,kind FROM verified_wechat_identities WHERE app_id=? AND openid=?', (app_id, openid)).fetchone()
        if old and (old[0] != unionid or old[1] != kind):
            raise ValueError('已核验的微信身份存在冲突，请核对开放平台绑定')
        users._conn.execute('INSERT INTO verified_wechat_identities VALUES(?,?,?,?,?) ON CONFLICT(app_id,openid) DO UPDATE SET verified_at=excluded.verified_at',
                            (app_id, openid, unionid, kind, time.time()))


def mini_for_unionid(users, unionid, app_id=''):
    with users._lock:
        if not _has_table(users, 'verified_wechat_identities'):
            return None
        query = "SELECT DISTINCT v.openid FROM verified_wechat_identities v JOIN users u ON u.openid=v.openid WHERE v.unionid=? AND v.kind='mini' AND u.account_type='wechat' AND (u.app_id='' OR u.app_id=v.app_id)"
        args = [unionid]
        if app_id:
            query += ' AND v.app_id=?'; args.append(app_id)
        rows = users._conn.execute(query, args).fetchall()
        # Never guess between two Mini Program owners with the same UnionID.
        return rows[0][0] if len(rows) == 1 else None


def bind_verified(users, web, mini, unionid='', evidence_app_id='', mini_app_id=''):
    if not unionid or not evidence_app_id or web == mini:
        raise ValueError('缺少微信身份核验，请重新授权')
    init_links(users)
    with users._lock, users._conn:
        db = users._conn
        db.execute('BEGIN IMMEDIATE')
        if users._purging:
            raise ValueError('账号清理中，请稍后再试')
        if not _has_table(users, 'web_credentials') or not db.execute('SELECT 1 FROM web_credentials WHERE account_id=?', (web,)).fetchone():
            raise ValueError('请先注册并登录网页账户')
        proof_sql="SELECT 1 FROM verified_wechat_identities v JOIN users u ON u.openid=v.openid WHERE v.openid=? AND v.unionid=? AND v.kind='mini' AND (u.app_id='' OR u.app_id=v.app_id)"
        proof_args=[mini,unionid]
        if mini_app_id:proof_sql+=' AND v.app_id=?';proof_args.append(mini_app_id)
        proof = db.execute(proof_sql,proof_args).fetchone()
        web_row = db.execute('SELECT account_type,balance,banned FROM users WHERE openid=?', (web,)).fetchone()
        mini_row = db.execute('SELECT account_type,balance,banned FROM users WHERE openid=?', (mini,)).fetchone()
        if not proof or not web_row or not mini_row or web_row[0] != 'web' or mini_row[0] != 'wechat':
            raise ValueError('尚未找到同一 UnionID 的小程序账户，请先在小程序完成微信登录')
        if web_row[2] or mini_row[2]:
            raise ValueError('账户状态异常，请联系后台处理')
        old = db.execute('SELECT canonical_openid,unionid FROM account_aliases WHERE alias_openid=?', (web,)).fetchone()
        if old:
            if old[0] != mini or old[1] != unionid:
                raise ValueError('该网页账户已经绑定其他微信账户')
            return {'canonical_openid': mini, 'aliases': aliases_unlocked(users, mini), 'transferred_amount': 0, 'already_bound': True}
        if db.execute('SELECT 1 FROM account_aliases WHERE canonical_openid=?', (mini,)).fetchone():
            raise ValueError('该微信已绑定另一个网页账户，请联系后台处理')
        if type(web_row[1]) is not int or type(mini_row[1]) is not int:
            raise ValueError('账户余额字段异常，请先由后台核对')
        amount = web_row[1]; combined=mini_row[1]+amount; now = time.time()
        if not -(2**63) <= combined < 2**63:
            raise ValueError('合并余额超出数据库整数范围，请由后台核对')
        db.execute('INSERT INTO account_aliases VALUES(?,?,?,?,?)', (web, mini, now, unionid, evidence_app_id))
        db.execute('UPDATE users SET balance=? WHERE openid=?', (combined, mini))
        db.execute('UPDATE users SET balance=0 WHERE openid=?', (web,))
        # A transfer is not a reward; retain both signed deltas for audit, while
        # the combined history can label the internal pair without inventing money.
        for identity, delta, peer in ((web, -amount, mini), (mini, amount, web)):
            db.execute('INSERT INTO audit(ts,openid,action,detail) VALUES(?,?,?,?)',
                       (now, identity, 'account_balance_transfer', 'delta='+str(delta)+' peer='+peer))
        return {'canonical_openid': mini, 'aliases': aliases_unlocked(users, mini), 'transferred_amount': amount, 'already_bound': False}
