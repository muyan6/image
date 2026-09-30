"""WeChat mini-program approval binds a browser to the same account/balance."""
import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel
import wechat_sec

COOKIE='wechat_browser_challenge'
PREFIX='/api/auth/wechat-web'
PAGE='pages/web-login/web-login'
TTL=300
DEFAULT_PUBLIC_ORIGIN='https://image.myil.top'


def normalized_origin(value):
    """Compare scheme/host/effective port, never paths or forwarded headers."""
    if not isinstance(value,str) or not value or any(c.isspace() or ord(c)<32 for c in value):return None
    try:
        u=urlsplit(value)
        if (u.scheme not in ('http','https') or not u.hostname or u.username is not None
                or u.password is not None or u.path not in ('','/') or u.query or u.fragment
                or u.netloc.endswith(':') or u.port == 0):return None
        port=u.port if u.port is not None else (443 if u.scheme=='https' else 80)
        return (u.scheme,u.hostname.lower(),port)
    except ValueError:return None


def browser_request(request):
    if request.headers.get('x-web-login')!='1':raise HTTPException(403,detail='请从本站登录')
    if request.headers.get('sec-fetch-site')=='cross-site':raise HTTPException(403,detail='登录请求来源不匹配')
    configured=os.environ.get('WEB_PUBLIC_ORIGIN',DEFAULT_PUBLIC_ORIGIN).strip()
    public=normalized_origin(configured)
    if public is None:raise HTTPException(503,detail='WEB_PUBLIC_ORIGIN 配置无效，请填写完整站点地址')
    origin=request.headers.get('origin')
    if origin is None:
        # Same-origin GETs often omit Origin. Keep the custom-header/cookie
        # binding; do not infer any authority from untrusted proxy headers.
        return normalized_origin(str(request.base_url))
    supplied=normalized_origin(origin)
    direct=normalized_origin(str(request.base_url))
    local=direct and direct[1] in ('localhost','127.0.0.1','::1')
    if supplied is None or not (supplied==public or (local and supplied==direct)):
        raise HTTPException(403,detail='登录请求来源不匹配')
    return supplied


class Approval(BaseModel):
    id:str
    action:str='approve'


class BrowserLogin:
    def __init__(self,path):
        self.lock=threading.RLock()
        self.db=sqlite3.connect(str(path),check_same_thread=False)
        self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('''CREATE TABLE IF NOT EXISTS browser_login(
            id TEXT PRIMARY KEY, secret TEXT, app_id TEXT, ip TEXT, created REAL,
            expires REAL, state TEXT, openid TEXT, qr BLOB, token TEXT)''')
        self.db.execute('CREATE INDEX IF NOT EXISTS browser_login_ip ON browser_login(ip,created)')
        self.db.commit()

    def create(self,app_id,ip):
        now=time.time();sid=secrets.token_hex(16);secret=secrets.token_hex(32)
        with self.lock,self.db:
            self.db.execute('DELETE FROM browser_login WHERE expires<?',(now-TTL,))
            count=self.db.execute('SELECT COUNT(*) FROM browser_login WHERE ip=? AND created>?',(ip,now-60)).fetchone()[0]
            if count>=5:raise HTTPException(429,detail='登录码请求过于频繁，请稍后再试')
            self.db.execute('INSERT INTO browser_login VALUES(?,?,?,?,?,?,?,?,?,?)',
                            (sid,hashlib.sha256(secret.encode()).hexdigest(),app_id,ip,now,now+TTL,'pending',None,None,None))
        return sid,secret

    def browser(self,sid,secret):
        with self.lock:
            row=self.db.execute('SELECT * FROM browser_login WHERE id=?',(sid,)).fetchone()
        if not row or not secret or not hmac.compare_digest(row['secret'],hashlib.sha256(secret.encode()).hexdigest()):
            raise HTTPException(403,detail='登录码不属于当前浏览器')
        if row['expires']<=time.time():raise HTTPException(410,detail='登录码已过期，请刷新')
        return dict(row)

    def approve(self,sid,openid,app_id,action):
        with self.lock,self.db:
            row=self.db.execute('SELECT * FROM browser_login WHERE id=?',(sid,)).fetchone()
            if not row or row['expires']<=time.time():raise HTTPException(410,detail='登录码已过期')
            if row['app_id']!=app_id:raise HTTPException(403,detail='登录码所属小程序已变更')
            if row['state']!='pending':raise HTTPException(409,detail='登录码已处理')
            changed=self.db.execute("UPDATE browser_login SET state=?,openid=? WHERE id=? AND state='pending'",
                                    ('approved' if action=='approve' else 'denied',openid,sid)).rowcount
            if changed!=1:raise HTTPException(409,detail='登录码已处理')


def make_web_login_router(runtime):
    router=APIRouter();holder={};guard=threading.Lock()

    def store():
        path=Path(runtime().jobs._db_path).parent/'web_login.db'
        with guard:
            if holder.get('path')!=path:
                if holder.get('store'):holder['store'].db.close()
                holder.update(path=path,store=BrowserLogin(path))
            return holder['store']

    def owned(request,sid):
        row=store().browser(sid,request.cookies.get(COOKIE,''))
        if row['app_id']!=runtime().settings.wechat().get('app_id'):
            raise HTTPException(410,detail='微信配置已变更，请刷新登录码')
        return row

    @router.post(PREFIX+'/start')
    def start(request:Request,response:Response):
        external=browser_request(request);conf=runtime().settings.wechat()
        if not conf.get('app_id') or not conf.get('app_secret'):raise HTTPException(503,detail='微信登录尚未配置')
        sid,secret=store().create(conf['app_id'],request.client.host if request.client else 'unknown')
        response.set_cookie(COOKIE,secret,max_age=TTL,httponly=True,secure=bool(external and external[0]=='https'),samesite='strict',path=PREFIX)
        response.headers['Cache-Control']='no-store'
        return {'id':sid,'expires_in':TTL,'qr_url':PREFIX+'/'+sid+'/qr'}

    @router.get(PREFIX+'/{sid}/qr')
    def qr(sid:str,request:Request):
        row=owned(request,sid);s=store()
        with s.lock:
            # Cache for this one short-lived session; simultaneous GETs cannot
            # multiply WeChat code-generation calls.
            cached=s.db.execute('SELECT qr FROM browser_login WHERE id=?',(sid,)).fetchone()[0]
            if not cached:
                try:
                    token=wechat_sec.get_access_token(runtime().settings)
                    r=requests.post('https://api.weixin.qq.com/wxa/getwxacodeunlimit',params={'access_token':token},
                                    json={'scene':sid,'page':PAGE,'check_path':True,'env_version':'release','width':280},timeout=(5,15))
                    r.raise_for_status()
                    data=r.content
                except (requests.RequestException,wechat_sec.WechatSecError):
                    raise HTTPException(503,detail='微信登录码暂时获取失败，请稍后刷新')
                if len(data)>512*1024 or not data.startswith((b'\x89PNG\r\n\x1a\n',b'\xff\xd8\xff')):
                    raise HTTPException(503,detail='微信未返回登录码，请确认小程序已发布网页登录确认页面')
                with s.db:s.db.execute('UPDATE browser_login SET qr=? WHERE id=?',(data,sid))
            else:data=cached
        return Response(data,media_type='image/png' if data.startswith(b'\x89PNG') else 'image/jpeg',headers={'Cache-Control':'no-store'})

    @router.get(PREFIX+'/{sid}/status')
    def status(sid:str,request:Request,response:Response):
        browser_request(request);row=owned(request,sid);response.headers['Cache-Control']='no-store'
        if row['state']=='denied':return {'state':'denied'}
        if row['state']!='approved':return {'state':'pending'}
        m=runtime();user=m.users.get_user(row['openid'])
        if not user or user.get('account_type')!='wechat':raise HTTPException(403,detail='微信账户已失效')
        with store().lock,store().db:
            current=store().db.execute('SELECT token FROM browser_login WHERE id=?',(sid,)).fetchone()[0]
            if not current:
                current=m.user_token(row['openid'])
                store().db.execute('UPDATE browser_login SET token=? WHERE id=?',(current,sid))
                m.users.audit(row['openid'],'web_login_approved')
        return {'state':'approved','token':current,'balance':m.users.get_balance(row['openid'])}

    @router.post(PREFIX+'/approve')
    def approve(payload:Approval,request:Request):
        m=runtime();user=m._current_user(request)
        if payload.action not in ('approve','deny'):raise HTTPException(400,detail='登录操作无效')
        store().approve(payload.id,user['openid'],m.settings.wechat().get('app_id') or '',payload.action)
        return {'ok':True}

    def close():
        with guard:
            if holder.get('store'):holder['store'].db.close()
            holder.clear()
    router.close=close
    return router
