"""Explicit zero-credit site accounts and revocable, random browser sessions.

No anonymous visitor creates a user. Passwords use salted PBKDF2-SHA256;
only session-token hashes are retained. Mini-program HMAC tokens never prove
registration of a legacy browser visitor.
"""
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
import time
from urllib.parse import urlsplit
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field
from user_store import invite_code_of, public_user_id

COOKIE = 'site_session'
SESSION_TTL = 7 * 24 * 3600
ITERATIONS = 600000
DEFAULT_ORIGIN = 'https://image.myil.top'
_init_lock = threading.Lock()


def password_hash(password):
    if not isinstance(password, str) or not 10 <= len(password) <= 128:
        raise ValueError('密码需要 10~128 个字符')
    salt = secrets.token_bytes(32)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, ITERATIONS)
    return 'pbkdf2_sha256$%d$%s$%s' % (ITERATIONS, salt.hex(), digest.hex())


def password_matches(password, encoded):
    try:
        name, iterations, salt, expected = encoded.split('$')
        rounds = int(iterations)
        if name != 'pbkdf2_sha256' or not 600000 <= rounds <= 2000000 or len(password) > 128:
            return False
        actual = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), bytes.fromhex(salt), rounds)
        return hmac.compare_digest(actual, bytes.fromhex(expected))
    except (ValueError, TypeError, AttributeError):
        return False


def normalize_username(username):
    username = username.strip().lower()
    if not re.fullmatch(r'[a-z0-9_]{3,32}', username):
        raise ValueError('用户名需要 3~32 位字母、数字或下划线')
    return username


def token_hash(token):
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def public_origin(settings=None):
    conf = settings.snapshot().get('web_wechat', {}) if settings else {}
    value = conf.get('public_origin') or os.environ.get('WEB_PUBLIC_ORIGIN', DEFAULT_ORIGIN)
    try:
        u = urlsplit(value); port=u.port
    except (ValueError,TypeError):raise HTTPException(503,detail='网站公开地址配置无效')
    if (u.scheme not in ('http', 'https') or not u.hostname or u.username is not None or u.password is not None
            or u.path not in ('', '/') or u.query or u.fragment or port==0
            or any(c.isspace() or ord(c)<32 for c in value) or u.netloc.endswith(':')):
        raise HTTPException(503, detail='网站公开地址配置无效')
    if u.scheme != 'https' and u.hostname not in ('localhost', '127.0.0.1', '::1'):
        raise HTTPException(503, detail='网站会话需要 HTTPS 地址')
    return value.rstrip('/')


def origin_key(value):
    try:
        u = urlsplit(value)
        if (u.scheme not in ('https', 'http') or not u.hostname or u.username is not None
                or u.password is not None or u.query or u.fragment or u.port==0 or u.netloc.endswith(':')):
            return None
        if u.path not in ('', '/') or any(c.isspace() for c in value): return None
        return (u.scheme, u.hostname.lower(), u.port if u.port is not None else (443 if u.scheme == 'https' else 80))
    except (ValueError, TypeError): return None


def site_request(request, settings=None):
    """A custom header and exact trusted Origin block cookie-based CSRF."""
    if request.headers.get('x-site-request') != '1':
        raise HTTPException(403, detail='请从本站操作账户')
    if request.headers.get('sec-fetch-site') == 'cross-site':
        raise HTTPException(403, detail='账户请求来源不匹配')
    trusted = origin_key(public_origin(settings)); supplied = request.headers.get('origin')
    direct = origin_key(str(request.base_url))
    local = direct and direct[1] in ('localhost', '127.0.0.1', '::1')
    if supplied is None:
        if not (local or request.headers.get('sec-fetch-site') == 'same-origin'):
            raise HTTPException(403, detail='账户请求缺少同源证明')
    elif origin_key(supplied) != trusted and not (local and origin_key(supplied) == direct):
        raise HTTPException(403, detail='账户请求来源不匹配')


def verify_csrf(request, session):
    supplied=request.headers.get('x-site-csrf') or request.headers.get('x-csrf-token')
    if supplied and not hmac.compare_digest(token_hash(supplied),session['csrf_hash']):
        raise HTTPException(403,detail='账户操作凭据已更新，请刷新后重试')


class WebAccounts:
    def __init__(self, users):
        self.users = users
        with users._lock, users._conn:
            users._conn.executescript('''
            CREATE TABLE IF NOT EXISTS web_credentials(
                account_id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS site_sessions(
                token_hash TEXT PRIMARY KEY, account_id TEXT NOT NULL, csrf_hash TEXT NOT NULL,
                created_at REAL NOT NULL, expires_at REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS site_sessions_owner ON site_sessions(account_id,expires_at);
            CREATE TABLE IF NOT EXISTS site_auth_attempts(
                id INTEGER PRIMARY KEY, ip_hash TEXT NOT NULL, kind TEXT NOT NULL,
                username TEXT NOT NULL, created_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS site_auth_attempt_ip ON site_auth_attempts(ip_hash,kind,created_at);
            CREATE TABLE IF NOT EXISTS site_oauth_states(
                state_hash TEXT PRIMARY KEY, account_id TEXT NOT NULL, session_hash TEXT NOT NULL,
                config_hash TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL,
                used INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS pending_wechat_links(
                account_id TEXT PRIMARY KEY, unionid TEXT NOT NULL UNIQUE,
                evidence_app_id TEXT NOT NULL, verified_at REAL NOT NULL);
            ''')

    def rate_limit(self, ip, kind, username=''):
        now = time.time(); ip_hash = token_hash(ip or 'unknown')
        window, maximum = (3600, 5) if kind == 'register' else (600, 30)
        with self.users._lock, self.users._conn:
            db = self.users._conn; db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM site_auth_attempts WHERE created_at<?', (now - 86400,))
            count = db.execute('SELECT COUNT(*) FROM site_auth_attempts WHERE ip_hash=? AND kind=? AND created_at>?',
                               (ip_hash, kind, now - window)).fetchone()[0]
            if count >= maximum:
                raise HTTPException(429, detail='账户操作过于频繁，请稍后再试')
            if kind == 'login' and username:
                per_user = db.execute("SELECT COUNT(*) FROM site_auth_attempts WHERE kind='login' AND username=? AND created_at>?",
                                      (username, now - window)).fetchone()[0]
                if per_user >= 40: raise HTTPException(429, detail='账户登录过于频繁，请稍后再试')
            db.execute('INSERT INTO site_auth_attempts(ip_hash,kind,username,created_at) VALUES(?,?,?,?)',
                       (ip_hash, kind, username, now))

    def _issue_unlocked(self, account_id):
        token = 'site_' + secrets.token_urlsafe(48); csrf = secrets.token_urlsafe(32); now = time.time()
        db = self.users._conn
        db.execute('DELETE FROM site_sessions WHERE expires_at<?', (now,))
        db.execute('UPDATE site_sessions SET revoked=1 WHERE token_hash IN '
                   '(SELECT token_hash FROM site_sessions WHERE account_id=? AND revoked=0 ORDER BY created_at DESC LIMIT -1 OFFSET 9)',
                   (account_id,))
        db.execute('INSERT INTO site_sessions(token_hash,account_id,csrf_hash,created_at,expires_at) VALUES(?,?,?,?,?)',
                   (token_hash(token), account_id, token_hash(csrf), now, now + SESSION_TTL))
        return token, csrf

    def register(self, username, password, ip):
        username = normalize_username(username); self.rate_limit(ip, 'register', username)
        encoded = password_hash(password); now = time.time(); account_id = 'web-' + secrets.token_hex(16)
        try:
            with self.users._lock, self.users._conn:
                if self.users._purging: raise ValueError('账号清理中，请稍后再试')
                db = self.users._conn; db.execute('BEGIN IMMEDIATE')
                db.execute('INSERT INTO users(openid,created_at,last_seen,balance,invite_code,account_type,nickname) VALUES(?,?,?,?,?,?,?)',
                           (account_id, now, now, 0, invite_code_of(account_id), 'web', username))
                db.execute('INSERT INTO web_credentials VALUES(?,?,?,?,?)', (account_id, username, encoded, now, now))
                db.execute('INSERT INTO audit(ts,openid,action,detail) VALUES(?,?,?,?)',
                           (now, account_id, 'site_registered', 'welcome=0'))
                token, csrf = self._issue_unlocked(account_id)
        except sqlite3.IntegrityError as exc:
            raise ValueError('用户名已被使用') from exc
        return account_id, token, csrf

    def registered(self, account_id):
        with self.users._lock:
            return bool(self.users._conn.execute('SELECT 1 FROM web_credentials WHERE account_id=?', (account_id,)).fetchone())

    def login(self, username, password, ip):
        username = normalize_username(username); self.rate_limit(ip, 'login', username)
        with self.users._lock:
            row = self.users._conn.execute('SELECT account_id,password_hash FROM web_credentials WHERE username=?', (username,)).fetchone()
        # Constant-cost missing usernames do not reveal registration via timing.
        encoded = row[1] if row else _dummy_hash()
        if not password_matches(password, encoded) or not row:
            raise ValueError('用户名或密码不正确')
        with self.users._lock, self.users._conn:
            db = self.users._conn; db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT password_hash FROM web_credentials WHERE account_id=?', (row[0],)).fetchone()
            if self.users._purging or not current or current[0] != encoded:
                raise ValueError('账户状态已变化，请重新登录')
            token, csrf = self._issue_unlocked(row[0])
            db.execute('INSERT INTO audit(ts,openid,action,detail) VALUES(?,?,?,?)', (time.time(), row[0], 'site_login', ''))
        return row[0], token, csrf

    def session(self, token):
        if not isinstance(token, str) or not token.startswith('site_') or not 40 <= len(token) <= 128:
            return None
        digest = token_hash(token)
        with self.users._lock:
            cur = self.users._conn.execute('SELECT s.*,c.username FROM site_sessions s JOIN web_credentials c ON c.account_id=s.account_id '
                'JOIN users u ON u.openid=s.account_id WHERE s.token_hash=? AND s.revoked=0 AND s.expires_at>?', (digest, time.time()))
            row = cur.fetchone()
            return dict(zip([c[0] for c in cur.description], row)) if row else None

    def revoke(self, token):
        with self.users._lock, self.users._conn:
            self.users._conn.execute('UPDATE site_sessions SET revoked=1 WHERE token_hash=?', (token_hash(token),))

    def refresh(self, token):
        session = self.session(token)
        if not session: raise ValueError('会话已失效，请重新登录')
        with self.users._lock, self.users._conn:
            db = self.users._conn; db.execute('BEGIN IMMEDIATE')
            active = db.execute('SELECT 1 FROM site_sessions WHERE token_hash=? AND revoked=0 AND expires_at>?',
                                (session['token_hash'], time.time())).fetchone()
            if not active: raise ValueError('会话已失效，请重新登录')
            db.execute('UPDATE site_sessions SET revoked=1 WHERE token_hash=?', (session['token_hash'],))
            token, csrf = self._issue_unlocked(session['account_id'])
        return session['account_id'], token, csrf

    def change_password(self, account_id, current, new):
        with self.users._lock:
            row = self.users._conn.execute('SELECT password_hash FROM web_credentials WHERE account_id=?', (account_id,)).fetchone()
        if not row or not password_matches(current, row[0]): raise ValueError('当前密码不正确')
        self.admin_reset(account_id, new, expected_hash=row[0])

    def admin_reset(self, identity, new_password, expected_hash=None):
        from account_links import aliases_unlocked
        encoded = password_hash(new_password)
        with self.users._lock, self.users._conn:
            db = self.users._conn; db.execute('BEGIN IMMEDIATE')
            owners = aliases_unlocked(self.users, identity)
            marks = ','.join('?' for _ in owners)
            row = db.execute('SELECT account_id,password_hash FROM web_credentials WHERE account_id IN (' + marks + ')', owners).fetchone()
            if not row or (expected_hash is not None and row[1] != expected_hash): raise ValueError('网站注册账户不存在或密码已变更')
            db.execute('UPDATE web_credentials SET password_hash=?,updated_at=? WHERE account_id=?', (encoded, time.time(), row[0]))
            db.execute('UPDATE site_sessions SET revoked=1 WHERE account_id=?', (row[0],))
            db.execute('INSERT INTO audit(ts,openid,action,detail) VALUES(?,?,?,?)', (time.time(), row[0], 'site_password_reset', 'sessions_revoked=1'))
        return {'ok': True, 'sessions_revoked': True}


_dummy = None
def _dummy_hash():
    global _dummy
    if _dummy is None: _dummy = password_hash('not-a-real-password-' + secrets.token_hex(8))
    return _dummy


def store_for(users):
    with _init_lock:
        if not getattr(users, '_web_accounts', None): users._web_accounts = WebAccounts(users)
        return users._web_accounts


def request_token(request):
    authorization = request.headers.get('authorization', '')
    if authorization.lower().startswith('bearer '): return authorization[7:].strip()
    return request.cookies.get(COOKIE, '')


def cookie(response, request, token, settings):
    external = public_origin(settings)
    direct=origin_key(str(request.base_url));supplied=origin_key(request.headers.get('origin'))
    loopback=bool(direct and direct[0]=='http' and direct[1] in ('localhost','127.0.0.1','::1')
                  and (supplied==direct or request.headers.get('origin') is None))
    response.set_cookie(COOKIE, token, max_age=SESSION_TTL, httponly=True,
                        secure=external.startswith('https://') and not loopback, samesite='lax', path='/')
    response.headers['Cache-Control'] = 'no-store'


def account_view(m, account_id, token=None, csrf=None):
    from account_links import canonical
    user = m.users.get_user(canonical(m.users, account_id))
    if not user: raise HTTPException(401, detail='账号已失效')
    with m.users._lock:
        row = m.users._conn.execute('SELECT username FROM web_credentials WHERE account_id=?', (account_id,)).fetchone()
        pending=bool(m.users._conn.execute('SELECT 1 FROM pending_wechat_links WHERE account_id=?',(account_id,)).fetchone())
    if not row: raise HTTPException(401, detail='请先注册网站账户')
    bound = user['openid'] != account_id
    conf = m.settings.snapshot().get('web_wechat', {})
    ready = bool(conf.get('enabled') and conf.get('app_id') and conf.get('app_secret') and conf.get('public_origin'))
    data = {'user_id': user['user_id'], 'auth_source': 'site', 'manual_credit_only': True,
            'account_type':'web',
            'account_user_id':public_user_id(account_id,'web'), 'site_username':row[0],
            'balance': user['balance'], 'username': row[0], 'wechat_bound': bound,
            'user': {'user_id': user['user_id'], 'username': row[0], 'nickname': user.get('nickname', ''),
                     'account_type': 'web', 'wechat_bound': bound},
            'wechat_login': {'enabled': bool(conf.get('enabled')), 'ready': ready,
                             'pending_mini_identity':pending,
                             'kind': conf.get('kind', 'official'),
                             'message': ('微信身份已核验，等待同一微信在小程序完成真实登录' if pending else
                                         '微信绑定可用' if ready else '微信绑定尚未配置，网站账户可独立使用')}}
    if token is not None: data['token'] = token
    if csrf is not None: data['csrf_token'] = csrf
    return data


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=10, max_length=128)


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=10, max_length=128)


def make_site_router(runtime):
    router = APIRouter()
    def authenticated(request):
        m = runtime(); token = request_token(request); session = store_for(m.users).session(token)
        if not session: raise HTTPException(401, detail='请先注册或登录网站账户')
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            site_request(request, m.settings);verify_csrf(request,session)
        return m, token, session

    @router.post('/api/auth/site/register')
    def register(body: Credentials, request: Request, response: Response):
        m = runtime(); site_request(request, m.settings)
        try: identity, token, csrf = store_for(m.users).register(body.username, body.password, request.client.host if request.client else 'unknown')
        except ValueError as exc: raise HTTPException(400, detail=str(exc)) from exc
        cookie(response, request, token, m.settings)
        return account_view(m, identity, token, csrf)

    @router.post('/api/auth/site/login')
    def login(body: Credentials, request: Request, response: Response):
        m = runtime(); site_request(request, m.settings)
        try: identity, token, csrf = store_for(m.users).login(body.username, body.password, request.client.host if request.client else 'unknown')
        except ValueError as exc: raise HTTPException(401, detail=str(exc)) from exc
        cookie(response, request, token, m.settings)
        return account_view(m, identity, token, csrf)

    @router.get('/api/auth/site/session')
    def session(request: Request, response: Response):
        m, token, session = authenticated(request); response.headers['Cache-Control'] = 'no-store'
        return {**account_view(m, session['account_id']), 'expires_at': session['expires_at']}

    @router.post('/api/auth/site/session')
    def refresh_session(request: Request, response: Response):
        m, old_token, session = authenticated(request)
        try: identity, token, csrf = store_for(m.users).refresh(old_token)
        except ValueError as exc: raise HTTPException(401, detail=str(exc)) from exc
        cookie(response, request, token, m.settings)
        return account_view(m, identity, token, csrf)

    @router.post('/api/auth/site/logout')
    def logout(request: Request, response: Response):
        m = runtime(); site_request(request, m.settings)
        token = request_token(request)
        if token: store_for(m.users).revoke(token)
        response.delete_cookie(COOKIE, path='/', httponly=True, secure=public_origin(m.settings).startswith('https://'), samesite='lax')
        response.headers['Cache-Control'] = 'no-store'
        return {'ok': True}

    @router.post('/api/auth/site/password')
    def password(body: PasswordChange, request: Request, response: Response):
        m, token, session = authenticated(request)
        try: store_for(m.users).change_password(session['account_id'], body.current_password, body.new_password)
        except ValueError as exc: raise HTTPException(400, detail=str(exc)) from exc
        response.delete_cookie(COOKIE, path='/'); return {'ok': True, 'login_required': True}

    return router
