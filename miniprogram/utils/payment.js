/* Payment success requests verification; only the backend's ledger changes balance. */
const api=require('./api.js');
const KEY='photonPendingCheckout';
function pending(){try{return wx.getStorageSync(KEY)||null;}catch(e){return null;}}
function remember(value){wx.setStorageSync(KEY,value);}
function clear(){try{wx.removeStorageSync(KEY);}catch(e){}}
function loginCode(){return new Promise((resolve,reject)=>wx.login({success:r=>r.code?resolve(r.code):reject(new Error('微信登录失败')),fail:reject}));}
function version(a,b){const x=String(a).split('.').map(Number),y=b.split('.').map(Number);for(let i=0;i<Math.max(x.length,y.length);i++){if((x[i]||0)!==(y[i]||0))return (x[i]||0)>(y[i]||0)?1:-1;}return 0;}
function supported(){
  if(typeof wx.requestVirtualPayment!=='function')throw new Error('当前微信环境不支持虚拟支付，请用手机微信最新版本');
  const info=wx.getSystemInfoSync?.()||{};
  if(info.platform==='devtools')throw new Error('请在手机微信验证支付，开发者工具仅用于构建与模拟测试');
  if(info.platform==='ios'&&(version(info.version||'0','8.0.68')<0||Number((info.system||'').match(/\d+/)?.[0]||0)<15))throw new Error('iOS 支付需要 iOS 15 及微信 8.0.68 或更新版本');
  return info;
}
function confirm(pkg,info){return new Promise(resolve=>wx.showModal({title:'确认购买光子权益',content:pkg.price_text+' · '+pkg.points+' 光子。通过官方虚拟支付购买图片生成权益，到账以服务端核实为准。'+(info.platform==='ios'?'iOS 由 Apple 收款，退款需向 App Store 申请。':'退款请联系客服核对订单，由开发者通过微信平台处理。'),success:r=>resolve(!!r.confirm),fail:()=>resolve(false)}));}
function invoke(data){return new Promise((resolve,reject)=>wx.requestVirtualPayment({...data,success:resolve,fail:e=>{
  const error=new Error(e.errMsg||'支付未完成');error.canceled=e.errCode===-2||/cancel/i.test(e.errMsg||'');reject(error);
}}));}
async function buy(pkg){
  const info=supported();
  if(info.platform==='ios'&&Number.isInteger(pkg.amount_fen)&&pkg.amount_fen<100)throw new Error('Apple 支付最低金额为 1 元，请选择其他套餐');
  await api.ensureLogin(true);
  const previous=pending();
  if(previous){const e=new Error('已有订单需要确认，请到充值订单中查看，核对后再购买');e.orderId=previous.id;throw e;}
  if(!await confirm(pkg,info))return {canceled:true};
  const code=await loginCode();const client_key='C'+Date.now().toString(36)+Math.random().toString(36).slice(2,14);
  remember({client_key,created:Date.now()});
  let created;
  try{created=await api.request('/api/payment/orders',{method:'POST',data:{package_id:pkg.id,code,client_key}});}
  catch(e){if(e.status&&e.status<500)clear();else e.message='下单状态待确认，请到充值订单查看，不重复下单';throw e;}
  const order=created.order;remember({id:order.id,client_key,created:Date.now()});
  if(created.existing||!created.pay_data)throw new Error('订单已登记，请在充值订单中核对');
  try{await invoke(created.pay_data);}
  catch(e){
    // A cancel/error may race payment completion. Keep the order, never mark it paid or refund locally.
    if(e.canceled)clear();else e.message='支付状态待确认，请先在充值订单中核对';
    e.orderId=order.id;throw e;
  }
  clear();
  await api.request('/api/payment/orders/'+encodeURIComponent(order.id)+'/sync',{method:'POST'}).catch(()=>{});
  return {orderId:order.id};
}
async function recover(){
  const p=pending();if(!p)return;
  const result=await api.request('/api/payment/orders?client_key='+encodeURIComponent(p.client_key));
  // The SDK was not retriggered. The order center preserves any registered order.
  clear();return result;
}
module.exports={buy,recover,pending,clear,supported,version};
