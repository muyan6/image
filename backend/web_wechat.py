"""Prepared official-account OAuth / Open Platform QRconnect binding.

Disabled by default. Only server-verified UnionIDs join an existing Mini Program
identity; absent mappings remain pending and never create a gifted account.
"""
import hashlib
import hmac
import json
import secrets
import time
from urllib.parse import urlencode
import requests
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from account_links import register_verified, mini_for_unionid, bind_verified
from web_accounts import (COOKIE, store_for, request_token, token_hash, site_request,
                          verify_csrf, public_origin, account_view)

CALLBACK = '/api/auth/site/wechat/callback'
TTL = 300


def configured(m):
    conf = m.settings.web_wechat()
    conf['mini_app_id']=m.settings.wechat().get('app_id') or ''
    if not conf.get('enabled'):
        raise HTTPException(503, detail='微信绑定尚未开启；网站账户可独立注册和使用')
    if not conf.get('app_id') or not conf.get('app_secret') or not conf.get('public_origin'):
        raise HTTPException(503, detail='微信绑定尚未配置公众号或开放平台网站应用')
    if conf.get('kind') not in ('official', 'open') or conf.get('callback_path') != CALLBACK:
        raise HTTPException(503, detail='微信绑定类型或回调配置无效')
    public_origin(m.settings)
    return conf


def config_hash(conf):
    # A pending state cannot switch applications/origins after an admin edit.
    raw = json.dumps({k: conf.get(k) for k in ('kind', 'app_id', 'app_secret', 'public_origin', 'callback_path','mini_app_id')}, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def _response_json(response):
    try:
        data = response.json()
    except (ValueError, TypeError) as exc:
        raise HTTPException(502, detail='微信身份接口返回异常') from exc
    if response.status_code != 200 or not isinstance(data, dict) or data.get('errcode') not in (None, 0):
        raise HTTPException(502, detail='微信身份验证未完成，请重新授权')
    return data


def exchange_code(conf, code):
    if not isinstance(code, str) or not 1 <= len(code) <= 200:
        raise HTTPException(400, detail='微信授权 code 无效')
    try:
        data = _response_json(requests.get('https://api.weixin.qq.com/sns/oauth2/access_token',
            params={'appid': conf['app_id'], 'secret': conf['app_secret'], 'code': code,
                    'grant_type': 'authorization_code'}, timeout=(5, 15)))
        openid = data.get('openid'); unionid = data.get('unionid')
        if not isinstance(openid, str) or not openid or len(openid) > 200 or not data.get('access_token'):
            raise HTTPException(502, detail='微信未返回完整身份凭据')
        # Official-account UnionID may be returned only by the authorized userinfo
        # call. Never replace it with OpenID or with a client-provided identity.
        if not unionid:
            info = _response_json(requests.get('https://api.weixin.qq.com/sns/userinfo',
                params={'access_token': data['access_token'], 'openid': openid, 'lang': 'zh_CN'}, timeout=(5, 15)))
            if info.get('openid') != openid:
                raise HTTPException(502, detail='微信身份接口返回不一致')
            unionid = info.get('unionid')
    except requests.RequestException as exc:
        raise HTTPException(503, detail='微信身份接口暂时未响应，请重新授权') from exc
    if not isinstance(unionid, str) or not unionid or len(unionid) > 200:
        raise HTTPException(409, detail='微信未返回 UnionID，请将公众号/网站应用与小程序绑定到同一开放平台账号')
    return openid, unionid


def pending_site_for_unionid(users, unionid):
    # Callers may perform real mini login before the site schema is initialized.
    with users._lock:
        exists = users._conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pending_wechat_links'").fetchone()
        if not exists: return None
        row = users._conn.execute('SELECT p.account_id FROM pending_wechat_links p JOIN web_credentials c '
                                  'ON c.account_id=p.account_id WHERE p.unionid=?', (unionid,)).fetchone()
        return row[0] if row else None


def complete_pending_link(users, mini, unionid, app_id):
    account_id = pending_site_for_unionid(users, unionid)
    if not account_id: return None
    with users._lock:
        row = users._conn.execute('SELECT evidence_app_id FROM pending_wechat_links WHERE account_id=? AND unionid=?',
                                  (account_id, unionid)).fetchone()
    if not row: return None
    try: result = bind_verified(users, account_id, mini, unionid, row[0],mini_app_id=app_id)
    except ValueError as exc: raise HTTPException(409, detail=str(exc)) from exc
    with users._lock, users._conn:
        users._conn.execute('DELETE FROM pending_wechat_links WHERE account_id=? AND unionid=?', (account_id, unionid))
    return result


def make_web_wechat_router(runtime):
    router = APIRouter()

    @router.post('/api/auth/site/wechat/start')
    def start(request: Request):
        m = runtime(); site_request(request, m.settings)
        store = store_for(m.users); session = store.session(request_token(request))
        if not session: raise HTTPException(401, detail='请先注册并登录网站账户')
        verify_csrf(request, session); conf = configured(m)
        state = secrets.token_urlsafe(32); now = time.time()
        store.rate_limit(request.client.host if request.client else 'unknown', 'wechat_start', session['account_id'])
        with m.users._lock, m.users._conn:
            db = m.users._conn; db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM site_oauth_states WHERE expires_at<?', (now,))
            db.execute('INSERT INTO site_oauth_states VALUES(?,?,?,?,?,?,0)',
                (token_hash(state), session['account_id'], session['token_hash'], config_hash(conf), now, now + TTL))
        endpoint = 'https://open.weixin.qq.com/connect/oauth2/authorize' if conf['kind'] == 'official' else 'https://open.weixin.qq.com/connect/qrconnect'
        scope = 'snsapi_userinfo' if conf['kind'] == 'official' else 'snsapi_login'
        url = endpoint + '?' + urlencode({'appid': conf['app_id'], 'redirect_uri': conf['public_origin'].rstrip('/') + CALLBACK,
            'response_type': 'code', 'scope': scope, 'state': state}) + '#wechat_redirect'
        return {'url': url, 'authorization_url': url, 'expires_in': TTL, 'kind': conf['kind']}

    @router.get(CALLBACK)
    def callback(request: Request, state: str = '', code: str = ''):
        m = runtime(); store = store_for(m.users); conf = configured(m)
        # A provider redirect is cross-site by design. Its one-use state must
        # instead match the same HttpOnly site session that started binding.
        session = store.session(request.cookies.get(COOKIE, ''))
        if not session: raise HTTPException(401, detail='绑定会话已失效，请登录后重新授权')
        if not state or len(state) > 128: raise HTTPException(403, detail='绑定授权状态无效')
        with m.users._lock, m.users._conn:
            db = m.users._conn; db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT account_id,session_hash,config_hash,expires_at,used FROM site_oauth_states WHERE state_hash=?',
                             (token_hash(state),)).fetchone()
            if (not row or row[4] or row[3] <= time.time() or row[0] != session['account_id']
                    or not hmac.compare_digest(row[1], session['token_hash']) or row[2] != config_hash(conf)):
                raise HTTPException(403, detail='绑定授权已失效或不属于当前账户')
            db.execute('UPDATE site_oauth_states SET used=1 WHERE state_hash=?', (token_hash(state),))
        openid, unionid = exchange_code(conf, code)
        try: register_verified(m.users, conf['app_id'], openid, unionid, conf['kind'])
        except ValueError as exc: raise HTTPException(409, detail=str(exc)) from exc
        mini = mini_for_unionid(m.users, unionid, conf['mini_app_id']) if conf['mini_app_id'] else None
        if mini:
            try: bind_verified(m.users, session['account_id'], mini, unionid, conf['app_id'],mini_app_id=conf['mini_app_id'])
            except ValueError as exc: raise HTTPException(409, detail=str(exc)) from exc
            outcome = 'bound'
        else:
            try:
                with m.users._lock, m.users._conn:
                    db = m.users._conn; db.execute('BEGIN IMMEDIATE')
                    previous = db.execute('SELECT unionid FROM pending_wechat_links WHERE account_id=?', (session['account_id'],)).fetchone()
                    if previous and previous[0] != unionid: raise ValueError('账户已核验另一微信身份，请联系后台处理')
                    db.execute('INSERT INTO pending_wechat_links VALUES(?,?,?,?) ON CONFLICT(account_id) '
                               'DO UPDATE SET verified_at=excluded.verified_at',
                               (session['account_id'], unionid, conf['app_id'], time.time()))
            except (ValueError, __import__('sqlite3').IntegrityError) as exc:
                raise HTTPException(409, detail='该微信身份已关联其他网站账户') from exc
            outcome = 'pending_mini_identity'
        m.users.audit(session['account_id'], 'site_wechat_verified', 'state=' + outcome)
        # No provider token/OpenID/UnionID is sent to the browser or URL.
        return RedirectResponse(conf['public_origin'].rstrip('/') + '/?wechat_binding=' + outcome,
                                status_code=303, headers={'Cache-Control': 'no-store'})

    @router.get('/api/auth/site/wechat/status')
    def status(request: Request):
        m = runtime(); session = store_for(m.users).session(request_token(request))
        if not session: raise HTTPException(401, detail='请先登录网站账户')
        result = account_view(m, session['account_id'])
        with m.users._lock:
            pending = bool(m.users._conn.execute('SELECT 1 FROM pending_wechat_links WHERE account_id=?', (session['account_id'],)).fetchone())
        return {**result['wechat_login'], 'bound': result['wechat_bound'], 'pending_mini_identity': pending}

    return router
