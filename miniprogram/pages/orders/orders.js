const api=require('../../utils/api.js');
const payment=require('../../utils/payment.js');
const LABELS={created:'待支付 / 待核实',delivered:'光子已到账',partial_refund:'部分退款',refunded:'已退款',closed:'订单已关闭'};
Page({
 data:{items:[],busy:false,error:'',balance:0,loaded:false},
 onLoad(o){this._selected=o.id||'';this._pollCount=0;},
 onShow(){this._active=true;this.loadOrders();},
 onHide(){this._active=false;clearTimeout(this._poll);},
 onUnload(){this._active=false;clearTimeout(this._poll);},
 async loadOrders(){
  if(this.data.busy)return;this.setData({busy:true,error:''});
  try{
   await payment.recover();
   if(this._selected)await api.request('/api/payment/orders/'+encodeURIComponent(this._selected)+'/sync',{method:'POST'}).catch(()=>{});
   const d=await api.request('/api/payment/orders');
   if(!this._active)return;
   const items=(d.items||[]).map(o=>({...o,statusLabel:LABELS[o.status]||'核对中',priceText:'¥'+(o.amount/100).toFixed(2),
       timeText:this.formatTime(o.created_at),refundText:'¥'+(o.refunded_fen/100).toFixed(2),
       statusTone:({delivered:'success',closed:'muted',refunded:'refund',partial_refund:'refund'})[o.status]||'pending'}));
   this.setData({items,balance:d.balance,loaded:true});
   clearTimeout(this._poll);
   if(this._selected&&this._pollCount++<6&&items.some(o=>o.id===this._selected&&o.status==='created'))
     this._poll=setTimeout(()=>{if(this._active)this.loadOrders();},5000);
  }catch(e){if(this._active)this.setData({error:e.message||'订单加载失败'});}
  finally{if(this._active)this.setData({busy:false});else this.data.busy=false;}
 },
 async onCheckOrder(e){
  if(this.data.busy)return;this._selected=e.currentTarget.dataset.id;this._pollCount=0;await this.loadOrders();
 },
 onGoCredits(){wx.navigateTo({url:'/pages/credits/credits'});},
 onCopyOrder(e){wx.setClipboardData({data:e.currentTarget.dataset.id});},
 formatTime(ts){const d=new Date(ts*1000),p=n=>String(n).padStart(2,'0');return `${d.getFullYear()}.${p(d.getMonth()+1)}.${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;}
});
