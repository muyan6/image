/* Bounded display-only caches. Prices, balances and paid submissions never use these helpers. */
const MEDIA_FIELDS=new Set(['cover','covers','thumbnail','thumbnailUrl','thumb_url','result_url','orig_url','resultUrl','origUrl']);
const clone=value=>JSON.parse(JSON.stringify(value));
export function mediaReusable(url,now=Date.now(),marginMs=10000){
  if(typeof url!=='string'||!url)return false;
  if(!/[?&]q-signature=/i.test(url))return true;
  try{const value=new URL(url,'https://invalid.local').searchParams.get('q-sign-time')||'',parts=value.split(';');return parts.length===2&&Number(parts[0])*1000<=now&&Number(parts[1])*1000>now+marginMs;}catch(e){return false;}
}
function identity(url){
  try{const u=new URL(url,'https://invalid.local');for(const key of [...u.searchParams.keys()])if(/^(q-sign-algorithm|q-ak|q-sign-time|q-key-time|q-header-list|q-url-param-list|q-signature|x-cos-security-token)$/i.test(key))u.searchParams.delete(key);return u.href;}catch(e){return url;}
}
export function stableMediaUrl(fresh,previous){return fresh&&previous&&identity(fresh)===identity(previous)&&mediaReusable(previous)?previous:fresh;}
export function reuseDisplayMedia(row,previous){
  const result={...row};if(!previous)return result;
  for(const key of MEDIA_FIELDS){if(typeof result[key]==='string')result[key]=stableMediaUrl(result[key],previous[key]);else if(Array.isArray(result[key]))result[key]=result[key].map((url,i)=>stableMediaUrl(url,(previous[key]||[])[i]));}return JSON.stringify(result)===JSON.stringify(previous)?previous:result;
}
function reusePayloadMedia(value,previous){
  if(!value||typeof value!=='object')return value;
  if(Array.isArray(value)){const old=new Map((Array.isArray(previous)?previous:[]).filter(row=>row&&row.id!=null).map(row=>[String(row.id),row]));return value.map((row,i)=>reusePayloadMedia(row,row&&row.id!=null?old.get(String(row.id)):(previous||[])[i]));}
  const result=reuseDisplayMedia(value,previous);for(const [key,item]of Object.entries(result))if(!MEDIA_FIELDS.has(key)&&item&&typeof item==='object')result[key]=reusePayloadMedia(item,previous&&previous[key]);return result;
}
function reusable(value,now){
  if(!value||typeof value!=='object')return true;
  if(value.status==='succeeded'&&value.expires_at&&Number(value.expires_at)*1000<=now)return false;
  for(const [key,item]of Object.entries(value)){if(MEDIA_FIELDS.has(key)){for(const url of Array.isArray(item)?item:[item])if(url&&!mediaReusable(url,now))return false;}else if(item&&typeof item==='object'&&!reusable(item,now))return false;}return true;
}
export function createDisplayCache({ttl=30000,maxEntries=64,now=()=>Date.now()}={}){
  const entries=new Map();
  const trim=()=>{while(entries.size>maxEntries){const [key,entry]=entries.entries().next().value;entry.invalid=true;entries.delete(key);}};
  return {
    async get(key,loader,{force=false}={}){
      let entry=entries.get(key);
      if(entry&&entry.promise)return clone(await entry.promise);
      if(!force&&entry&&now()<entry.until&&reusable(entry.value,now())){entries.delete(key);entries.set(key,entry);return clone(entry.value);}
      const previous=entry&&entry.value;if(entry)entry.invalid=true;
      entry={invalid:false,until:0,promise:null};entries.set(key,entry);trim();
      entry.promise=Promise.resolve().then(loader).then(value=>{if(entry.invalid||entries.get(key)!==entry)throw Object.assign(new Error('展示记录已更新，请重新读取'),{code:'DISPLAY_STALE'});entry.value=clone(reusePayloadMedia(value,previous));entry.until=now()+ttl;entry.promise=null;return entry.value;},error=>{if(entries.get(key)===entry)entries.delete(key);throw error;});
      try{return clone(await entry.promise);}catch(error){if(entries.get(key)===entry)entries.delete(key);throw error;}
    },
    invalidate(prefix=''){for(const [key,entry]of entries)if(!prefix||key.startsWith(prefix)||key.includes('\n'+prefix)){entry.invalid=true;entries.delete(key);}},
    clear(){this.invalidate();},get size(){return entries.size;}
  };
}
/* Keep unchanged cards (and their image nodes) instead of rebuilding an entire grid. */
export function reconcileById(host,rows,render){
  const old=host._displayCards||new Map(),next=new Map(),nodes=[];
  for(const row of rows){const key=String(row.id),signature=JSON.stringify(row),prior=old.get(key);let node;
    if(prior&&prior.signature===signature)node=prior.node;
    else {node=render(row);if(prior&&node.querySelectorAll&&prior.node.querySelectorAll){const images=[...node.querySelectorAll('img')],before=[...prior.node.querySelectorAll('img')];images.forEach((img,i)=>{const existing=before[i];if(existing&&existing.getAttribute('src')===img.getAttribute('src')&&existing.getAttribute('alt')===img.getAttribute('alt')&&img.replaceWith)img.replaceWith(existing);});}}
    next.set(key,{node,signature});nodes.push(node);
  }
  // Fallback supports the intentionally small DOM used by creation regression tests.
  if(!host.insertBefore||!host.removeChild)host.replaceChildren(...nodes);
  else {let cursor=host.firstChild;for(const node of nodes){if(node===cursor)cursor=cursor.nextSibling;else host.insertBefore(node,cursor);}while(cursor){const nextNode=cursor.nextSibling;host.removeChild(cursor);cursor=nextNode;}}
  host._displayCards=next;
}
export function createVisibilityGate(doc,active=()=>true){
  let closed=false;const waits=new Set(),listeners=new Set();
  const visible=()=>!closed&&active()&&(!doc||doc.visibilityState!=='hidden');
  const change=()=>{if(visible()){waits.forEach(resolve=>resolve());waits.clear();}listeners.forEach(fn=>fn(visible()));};
  if(doc&&doc.addEventListener)doc.addEventListener('visibilitychange',change);
  return {visible,wait(){if(visible()||closed||!active())return Promise.resolve();return new Promise(resolve=>waits.add(resolve));},subscribe(fn){listeners.add(fn);return ()=>listeners.delete(fn);},close(){closed=true;if(doc&&doc.removeEventListener)doc.removeEventListener('visibilitychange',change);waits.forEach(resolve=>resolve());waits.clear();listeners.clear();}};
}
/* Separate from UI/poll-delay timers; the signal also bounds response body reads. */
export function armReadTimeout(controller,deadline){
  if(!controller)return ()=>{};
  const remaining=Math.min(15000,Math.max(0,deadline-Date.now()));
  if(!remaining){controller.abort();return ()=>{};}
  const timer=setTimeout(()=>controller.abort(),remaining);return ()=>clearTimeout(timer);
}
