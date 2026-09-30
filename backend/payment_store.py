"""Orders and entitlement adjustments use the SAME SQLite transaction as user balance."""
import json,time,uuid


class PaymentStore:
    def __init__(self,users):
        self.users=users
        with users._lock,users._conn:
            users._conn.executescript('''
            CREATE TABLE IF NOT EXISTS payment_orders(
              id TEXT PRIMARY KEY,openid TEXT NOT NULL,client_key TEXT NOT NULL,
              appid TEXT NOT NULL,offerid TEXT NOT NULL,env INTEGER NOT NULL,
              package_id TEXT NOT NULL,amount INTEGER NOT NULL,points INTEGER NOT NULL,
              status TEXT NOT NULL DEFAULT 'created',wx_order_id TEXT,
              created_at REAL NOT NULL,updated_at REAL NOT NULL,credited INTEGER NOT NULL DEFAULT 0,
              refunded_fen INTEGER NOT NULL DEFAULT 0,reversed_points INTEGER NOT NULL DEFAULT 0,
              next_sync REAL NOT NULL DEFAULT 0,provided INTEGER NOT NULL DEFAULT 0,
              sign_data TEXT NOT NULL,error TEXT NOT NULL DEFAULT '',
              UNIQUE(openid,client_key),UNIQUE(appid,env,wx_order_id));
            CREATE INDEX IF NOT EXISTS idx_payment_user ON payment_orders(openid,created_at);
            CREATE TABLE IF NOT EXISTS payment_ledger(
              id INTEGER PRIMARY KEY,order_id TEXT NOT NULL,openid TEXT NOT NULL,
              action TEXT NOT NULL,delta INTEGER NOT NULL,refund_total INTEGER NOT NULL,
              created_at REAL NOT NULL,UNIQUE(order_id,action,refund_total));
            ''')
    def _one(self,query,args):
        cur=self.users._conn.execute(query,args);row=cur.fetchone()
        return dict(zip([x[0] for x in cur.description],row)) if row else None
    def get(self,jid,openid=None):
        with self.users._lock:
            return self._one('SELECT * FROM payment_orders WHERE id=?'+(' AND openid=?' if openid else ''),(jid,openid) if openid else (jid,))
    def find(self,openid,key):
        with self.users._lock:return self._one('SELECT * FROM payment_orders WHERE openid=? AND client_key=?',(openid,key))
    def create(self,openid,key,app,offer,env,package):
        now=time.time();jid='P'+uuid.uuid4().hex[:30]
        data={'offerId':offer,'buyQuantity':1,'env':env,'currencyType':'CNY','productId':package['id'],
              'goodsPrice':package['yuan']*100,'outTradeNo':jid,'attach':jid}
        raw=json.dumps(data,ensure_ascii=False,separators=(',',':'))
        with self.users._lock,self.users._conn:
            self.users._conn.execute('BEGIN IMMEDIATE')
            existing=self._one('SELECT * FROM payment_orders WHERE openid=? AND client_key=?',(openid,key))
            if existing:return existing,False
            count=self.users._conn.execute('SELECT COUNT(*) FROM payment_orders WHERE openid=? AND created_at>?',(openid,now-60)).fetchone()[0]
            if count>=5:raise ValueError('下单过于频繁，请稍后再试')
            self.users._conn.execute('INSERT INTO payment_orders(id,openid,client_key,appid,offerid,env,package_id,amount,points,created_at,updated_at,next_sync,sign_data) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
               (jid,openid,key,app,offer,env,package['id'],data['goodsPrice'],package['points'],now,now,now+30,raw))
            return self._one('SELECT * FROM payment_orders WHERE id=?',(jid,)),True
    def apply_verified(self,jid,wxid,paid,refund_fen=0):
        """Called ONLY after authoritative WeChat query and snapshot matching."""
        u=self.users;now=time.time()
        with u._lock,u._conn:
            u._conn.execute('BEGIN IMMEDIATE');o=self._one('SELECT * FROM payment_orders WHERE id=?',(jid,))
            if not o:raise ValueError('订单不存在')
            if not wxid or (o['wx_order_id'] and wxid!=o['wx_order_id']):raise ValueError('平台订单号不匹配')
            if not 0<=refund_fen<=o['amount']:raise ValueError('退款金额异常')
            if refund_fen<o['refunded_fen']:refund_fen=o['refunded_fen'] # stale paid notification cannot undo refund
            u._conn.execute('UPDATE payment_orders SET wx_order_id=? WHERE id=?',(wxid,jid))
            # A refund observed before delivery never creates usable credits.
            if paid and not o['credited'] and refund_fen<o['amount']:
                cur=u._conn.execute('UPDATE users SET balance=balance+? WHERE openid=?',(o['points'],o['openid']))
                if cur.rowcount!=1:raise ValueError('订单账户不存在，暂停发货')
                u._conn.execute('INSERT INTO payment_ledger(order_id,openid,action,delta,refund_total,created_at) VALUES(?,?,?,?,?,?)',(jid,o['openid'],'credit',o['points'],0,now))
                o['credited']=1
            reversed_points=o['points']*refund_fen//o['amount'] if o['credited'] else 0
            delta=reversed_points-o['reversed_points']
            if delta>0:
                # Negative balances retain refund debt rather than silently forgiving spent entitlements.
                u._conn.execute('UPDATE users SET balance=balance-? WHERE openid=?',(delta,o['openid']))
                u._conn.execute('INSERT INTO payment_ledger(order_id,openid,action,delta,refund_total,created_at) VALUES(?,?,?,?,?,?)',(jid,o['openid'],'refund',-delta,refund_fen,now))
            status='refunded' if refund_fen==o['amount'] else ('partial_refund' if refund_fen else ('delivered' if o['credited'] else 'created'))
            u._conn.execute('UPDATE payment_orders SET credited=?,status=?,refunded_fen=?,reversed_points=?,updated_at=?,next_sync=?,error=? WHERE id=?',
                (o['credited'],status,refund_fen,reversed_points,now,now+3600,'',jid))
            return self._one('SELECT * FROM payment_orders WHERE id=?',(jid,))
    def update(self,jid,**values):
        allowed={'error','next_sync','provided','status'}
        if not values or not set(values)<=allowed:raise ValueError('订单更新字段无效')
        with self.users._lock,self.users._conn:
            self.users._conn.execute('UPDATE payment_orders SET '+','.join(k+'=?' for k in values)+',updated_at=? WHERE id=?',(*values.values(),time.time(),jid))
    def list(self,openid,client_key='',limit=50):
        with self.users._lock:
            cur=self.users._conn.execute('SELECT * FROM payment_orders WHERE openid=?'+(' AND client_key=?' if client_key else '')+' ORDER BY created_at DESC LIMIT ?',
                 (openid,client_key,limit) if client_key else (openid,limit))
            columns=[x[0] for x in cur.description];return [dict(zip(columns,r)) for r in cur.fetchall()]
    def due(self,limit=5):
        with self.users._lock:
            cur=self.users._conn.execute("SELECT id FROM payment_orders WHERE next_sync<=? AND status NOT IN ('refunded','closed') ORDER BY next_sync LIMIT ?",(time.time(),limit));return [r[0] for r in cur.fetchall()]
