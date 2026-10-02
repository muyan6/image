"""Only authenticated owners may create/read orders; callbacks verify transport and query WeChat."""
import json,re,time,xml.etree.ElementTree as ET
from fastapi import APIRouter,HTTPException,Request
from fastapi.responses import JSONResponse,PlainTextResponse,Response
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel
from virtual_payment import public_order
from wechat_auth import WechatAuthError
from wechat_sec import verify_push_signature

class Checkout(BaseModel):
    package_id:str
    code:str
    client_key:str

def parse_message(raw):
    if len(raw)>128*1024:raise ValueError('推送内容过大')
    if raw.lstrip().startswith(b'<'):
        if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():raise ValueError('XML 格式无效')
        def node(n):return {x.tag:node(x) for x in n} if len(n) else (n.text or '')
        value=node(ET.fromstring(raw))
    else:value=json.loads(raw)
    if not isinstance(value,dict) or value.get('Encrypt'):raise ValueError('请配置明文消息推送')
    return value

async def handle_message(m,request):
    token=m.settings.moderation().get('wechat_push_token','');q=request.query_params
    if not verify_push_signature(token,q.get('signature',''),q.get('timestamp',''),q.get('nonce','')):
        raise HTTPException(403,detail='微信推送签名无效')
    if request.method=='GET':return PlainTextResponse(q.get('echostr',''))
    chunks=[];total=0
    async for chunk in request.stream():
        total+=len(chunk)
        if total>128*1024:raise HTTPException(413,detail='推送内容过大')
        chunks.append(chunk)
    raw=b''.join(chunks)
    try:
        body=parse_message(raw);event=body.get('Event','')
        if event.startswith('xpay_'):
            result=await run_in_threadpool(m.payments.notification,body)
            if result:return JSONResponse(result)
        else:
            parsed=m.wechat_sec.parse_push_body(body)
            if parsed:
                trace,suggest,label,score=parsed;m.cloud.audits.wechat_callback(trace,suggest)
                m.wechat_sec.resolve_pending(trace,suggest,label,score)
        if raw.lstrip().startswith(b'<'):return Response('<xml><ErrCode>0</ErrCode><ErrMsg>success</ErrMsg></xml>',media_type='application/xml')
        return JSONResponse({'ErrCode':0,'ErrMsg':'success'}) if event.startswith('xpay_') else PlainTextResponse('success')
    except (ValueError,KeyError,TypeError,ET.ParseError) as exc:
        m.log.warning('微信推送暂未完成核对：%s',type(exc).__name__)
        if raw.lstrip().startswith(b'<'):return Response('<xml><ErrCode>1</ErrCode><ErrMsg>verification pending</ErrMsg></xml>',media_type='application/xml')
        return JSONResponse({'ErrCode':1,'ErrMsg':'verification pending'},status_code=400)

def make_payment_router(runtime):
    r=APIRouter()
    @r.post('/api/payment/orders')
    def create_order(body:Checkout,request:Request):
        m=runtime();u=m._current_user(request)
        if u.get('auth_source')=='site':raise HTTPException(403,detail='网页光子仅由后台手动增加，暂不开放在线充值')
        if not re.fullmatch(r'[A-Za-z0-9_-]{12,80}',body.client_key) or not 1<=len(body.code)<=200:raise HTTPException(400,detail='下单参数无效')
        try:return m.payments.create(u,body.package_id,body.code,body.client_key)
        except (ValueError,WechatAuthError) as exc:raise HTTPException(400,detail=str(exc)) from exc
    @r.get('/api/payment/orders')
    def list_orders(request:Request,client_key:str='',limit:int=50,offset:int=0):
        m=runtime();u=m._current_user(request)
        limit=max(1,min(100,limit));offset=max(0,offset)
        orders=m.payments.store().list(u['openid'],client_key,limit+1,offset)
        items=[public_order(o) for o in orders[:limit]]
        return {'items':items,'balance':m.users.get_balance(u['openid']),'has_more':len(orders)>limit,'next_offset':offset+len(items)}
    @r.get('/api/payment/orders/{jid}')
    def get_order(jid:str,request:Request):
        m=runtime();u=m._current_user(request);o=m.payments.store().get(jid,u['openid'])
        if not o:raise HTTPException(404,detail='订单不存在')
        return {'order':public_order(o),'balance':m.users.get_balance(u['openid'])}
    @r.post('/api/payment/orders/{jid}/sync')
    def sync_order(jid:str,request:Request):
        m=runtime();u=m._current_user(request);o=m.payments.store().get(jid,u['openid'])
        if not o:raise HTTPException(404,detail='订单不存在')
        try:
            # Frontend success merely requests an authoritative check; it never credits by itself.
            if time.time()-o['updated_at']>=5:m.payments.sync(jid,force=True)
        except ValueError:pass
        return {'order':public_order(m.payments.store().get(jid)),'balance':m.users.get_balance(u['openid'])}
    @r.api_route('/api/payment/notify',methods=['GET','POST'])
    async def notify(request:Request):return await handle_message(runtime(),request)
    return r
