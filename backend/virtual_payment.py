"""WeChat goods payment: server signatures, authoritative verification and durable delivery."""
import hashlib,hmac,json,threading,time
import requests
from payment_store import PaymentStore
from credit_packages import PACKAGES
from wechat_auth import exchange_session,WechatAuthError
from wechat_sec import get_access_token


def signature(key,message):return hmac.new(key.encode('utf-8'),message.encode('utf-8'),hashlib.sha256).hexdigest()
def integer(value):
    if isinstance(value,bool) or isinstance(value,float):raise ValueError('支付字段类型无效')
    return int(value)
def public_order(o):
    return {k:o[k] for k in ('id','package_id','amount','points','env','status','created_at','updated_at','credited','refunded_fen','reversed_points','error')}


class VirtualPayment:
    def __init__(self,runtime):
        self.runtime=runtime;self.lock=threading.RLock();self._users=None;self._store=None
        self.stop_event=threading.Event();self.thread=None;self.sync_lock=threading.Lock()
        self.syncing=set();self.query_slots=threading.BoundedSemaphore(8)
    def store(self):
        with self.lock:
            users=self.runtime().users
            if users is not self._users:self._store=PaymentStore(users);self._users=users
            return self._store
    def conf(self):return self.runtime().settings.snapshot()['payment']
    def ready(self):
        m=self.runtime();c=self.conf();wx=m.settings.wechat()
        return bool(c.get('enabled') and c.get('offer_id') and self.key(c,c.get('env',0)) and
                    wx.get('app_id') and wx.get('app_secret') and m.settings.moderation().get('wechat_push_token'))
    def key(self,conf,env):return str(conf.get('production_app_key' if env==0 else 'sandbox_app_key') or '')
    def create(self,user,package_id,code,client_key):
        m=self.runtime();store=self.store()
        if not self.ready():raise ValueError('充值未启用或微信支付配置未齐全')
        if user.get('account_type')!='wechat' or user.get('banned'):raise ValueError('请使用正常的小程序微信账号充值')
        package=next((p for p in PACKAGES if p['id']==package_id),None)
        if not package:raise ValueError('充值商品不存在')
        existing=store.find(user['openid'],client_key)
        if existing:return {'existing':True,'order':public_order(existing)}
        wx=m.settings.wechat();conf=self.conf();app=str(wx['app_id']);offer=str(conf['offer_id']);env=int(conf.get('env',0))
        session=exchange_session(code,m.settings)
        if session['openid']!=user['openid']:raise ValueError('支付登录账号不一致，请重新登录')
        if not session.get('session_key'):raise ValueError('微信未返回有效支付登录态')
        if m.settings.wechat()['app_id']!=app or self.conf()!=conf:raise ValueError('支付配置已变更，请重新下单')
        order,new=store.create(user['openid'],client_key,app,offer,env,package)
        if not new:return {'existing':True,'order':public_order(order)}
        raw=order['sign_data']
        return {'order':public_order(order),'pay_data':{'mode':'short_series_goods','signData':raw,
                'paySig':signature(self.key(conf,env),'requestVirtualPayment&'+raw),
                'signature':signature(session['session_key'],raw)}}
    def call(self,path,body,order):
        m=self.runtime();conf=self.conf()
        if conf.get('offer_id')!=order['offerid'] or m.settings.wechat()['app_id']!=order['appid']:
            raise ValueError('订单所属支付应用已变更，暂停核对')
        key=self.key(conf,order['env'])
        if not key:raise ValueError('订单环境的支付密钥未配置')
        raw=json.dumps(body,ensure_ascii=False,separators=(',',':'))
        params={'access_token':get_access_token(m.settings)}
        if path=='/xpay/query_order':params['pay_sig']=signature(key,path+'&'+raw)
        try:
            r=requests.post('https://api.weixin.qq.com'+path,params=params,data=raw.encode('utf-8'),
                            headers={'Content-Type':'application/json'},timeout=(5,15),allow_redirects=False)
            if r.status_code!=200:raise ValueError('微信订单接口 HTTP '+str(r.status_code))
            if path=='/xpay/notify_provide_goods' and not r.content:return {}
            data=r.json()
        except requests.RequestException as exc:raise ValueError('微信订单接口暂未响应，订单保留待核对') from exc
        if not isinstance(data,dict) or data.get('errcode',0 if path=='/xpay/notify_provide_goods' else None)!=0:
            raise ValueError('微信订单接口错误 '+str(data.get('errcode') if isinstance(data,dict) else 'invalid'))
        return data
    def sync(self,jid,force=False,notify=True):
        store=self.store();o=store.get(jid)
        if not o:raise ValueError('订单不存在')
        if not force and o['next_sync']>time.time():return o
        with self.sync_lock:
            if jid in self.syncing or not self.query_slots.acquire(blocking=False):return o
            self.syncing.add(jid)
        try:
            store.update(jid,next_sync=time.time()+30)
            data=self.call('/xpay/query_order',{'openid':o['openid'],'env':o['env'],'order_id':jid},o)
            remote=data.get('order')
            if not isinstance(remote,dict) or remote.get('order_id')!=jid:raise ValueError('微信订单号核对失败')
            status=integer(remote['status'])
            if status not in (2,3,4,5,8):
                if status==6 and not o['credited']:store.update(jid,status='closed',next_sync=time.time()+86400)
                else:store.update(jid,next_sync=time.time()+300,error='')
                return store.get(jid)
            if integer(remote.get('order_type',-1)) not in (0,7):raise ValueError('微信返回的不是原支付订单')
            if integer(remote.get('order_fee',-1))!=o['amount'] or integer(remote.get('paid_fee',-1))!=o['amount']:
                raise ValueError('支付金额与订单快照不一致')
            if integer(remote.get('env_type',-1))!=o['env']+1:raise ValueError('订单支付环境不匹配')
            wxid=str(remote.get('wx_order_id') or '')
            if not wxid:raise ValueError('微信订单未返回平台单号')
            remaining=integer(remote.get('left_fee',o['amount'] if status in (2,3,4) else -1))
            if not 0<=remaining<=o['amount']:raise ValueError('微信退款状态待核对')
            refund=o['amount']-remaining
            if status in (5,8) and refund==0:raise ValueError('微信退款金额尚未确认')
            updated=store.apply_verified(jid,wxid,True,refund)
            if status==4:store.update(jid,provided=1)
            elif notify and updated['credited'] and updated['refunded_fen']==0 and not updated['provided']:
                self.call('/xpay/notify_provide_goods',{'order_id':jid,'env':o['env']},o)
                store.update(jid,provided=1)
            return store.get(jid)
        except Exception as exc:
            store.update(jid,next_sync=time.time()+300,error=str(exc)[:200]);raise
        finally:
            with self.sync_lock:self.syncing.discard(jid)
            self.query_slots.release()
    def notification(self,body):
        event=body.get('Event');store=self.store()
        if event=='xpay_goods_deliver_notify':
            jid=body.get('OutTradeNo','');o=store.get(jid)
            if not o:raise ValueError('发货订单不存在')
            goods=body.get('GoodsInfo') or {}
            if (body.get('OpenId')!=o['openid'] or integer(body.get('Env',-1))!=o['env'] or
                goods.get('ProductId')!=o['package_id'] or integer(goods.get('Quantity',0))!=1 or
                integer(goods.get('OrigPrice',-1))!=o['amount'] or integer(goods.get('ActualPrice',-1))!=o['amount']):
                raise ValueError('发货通知与订单快照不一致')
            wxid=(body.get('WeChatPayInfo') or {}).get('MchOrderNo')
            verified=self.sync(jid,force=True,notify=False)
            if verified['status'] in ('refunded','partial_refund'):return
            if not verified['credited'] or (wxid and verified['wx_order_id']!=wxid):raise ValueError('订单尚未通过微信核对发货')
            store.update(jid,provided=1)
        elif event=='xpay_refund_notify':
            if integer(body.get('RetCode',-1))!=0:return
            o=store.get(body.get('MchOrderId',''))
            if not o or body.get('OpenId')!=o['openid']:raise ValueError('退款订单不匹配')
            verified=self.sync(o['id'],force=True)
            if verified['refunded_fen']<integer(body.get('RefundFee',0)):raise ValueError('退款金额尚未核实')
        elif event=='xpay_subscribe_ios_refund_query_notify':
            # No unsupported automatic refund adjudication; platform refund completion is reconciled below.
            raise ValueError('iOS 退款问询需要人工核对，实际退款完成后自动对账')
    def start(self):
        if self.thread and self.thread.is_alive():return
        self.stop_event.clear();self.thread=threading.Thread(target=self.loop,name='payment-reconcile',daemon=True);self.thread.start()
    def stop(self):
        self.stop_event.set()
        if self.thread:self.thread.join(timeout=2)
    def loop(self):
        while not self.stop_event.wait(30):
            for jid in self.store().due():
                if self.stop_event.is_set():break
                try:self.sync(jid)
                except Exception:self.runtime().log.warning('支付订单核对待重试：%s',jid)
