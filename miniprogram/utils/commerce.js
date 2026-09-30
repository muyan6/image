/** Display-only offer metadata. Checkout amounts and entitlements stay on the server. */
function countdown(seconds) {
  const value=Math.max(0,Math.ceil(seconds));
  const days=Math.floor(value/86400),hours=Math.floor(value%86400/3600);
  const pad=n=>String(n).padStart(2,'0');
  return (days?days+'天 ':'')+pad(hours)+':'+pad(Math.floor(value%3600/60))+':'+pad(value%60);
}
function decorate(packages,now) {
  return packages.map(p=>{
    const active=p.promotion_active===true&&Number(p.promotion_ends_at)>now;
    const discounted=active&&Number.isInteger(p.amount_fen)&&Number.isInteger(p.regular_amount_fen)&&p.amount_fen<p.regular_amount_fen;
    return {...p,promotionVisible:active,originalPrice:discounted?p.regular_price_text:'',
      promotionLabel:discounted?'限时特惠':'限时加赠',
      countdownText:active?countdown(p.promotion_ends_at-now):''};
  });
}
module.exports={countdown,decorate};
