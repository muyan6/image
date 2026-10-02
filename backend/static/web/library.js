/* Shared-account library. All user data comes from authenticated server APIs. */
const CATEGORIES=[['all','全部展品'],['portrait','冷白人像'],['film','复古胶片'],['old_photo','老照片复苏'],['anime','动漫重绘']];
const STATUS={processing:'生成中',succeeded:'已完成',failed:'失败'};
const SUBMISSIONS={uploading:'保存中，可继续提交',pending:'等待审核',published:'已发布',rejected:'未通过 / 已下架',withdrawn:'已撤回'};
const ORDER_STATUS={created:'待支付 / 待核实',delivered:'光子已到账',partial_refund:'部分退款',refunded:'已退款',closed:'订单已关闭'};
const REPORTS=[['spam','广告引流'],['abuse','辱骂攻击'],['inappropriate','不适宜内容'],['privacy','侵犯隐私'],['copyright','侵权'],['other','其他问题']];
const dateLabel=ts=>ts?new Date(Number(ts)*1000).toLocaleString('zh-CN',{hour12:false}):'时间未记录';
const encode=value=>encodeURIComponent(String(value||''));
function mediaUrl(value,baseURI){
  if(typeof value!=='string'||!value)return '';
  try{const u=new URL(value,baseURI||(typeof document!=='undefined'?document.baseURI:'http://localhost/'));return ['http:','https:'].includes(u.protocol)?u.href:'';}catch(e){return '';}
}
function settlement(job){
  const charged=job.charged_amount,refunded=job.refunded_amount;
  return Number.isInteger(charged)&&Number.isInteger(refunded)?`扣除 ${charged} · 已退回 ${refunded} 光子`:'历史扣退金额未完整记录，请查看光子明细';
}
function expiry(job){
  if(!job.expires_at)return '';
  const left=Number(job.expires_at)*1000-Date.now();
  return left<=0?'云端图片已到期':left<=3*86400000?`还剩 ${Math.max(1,Math.ceil(left/86400000))} 天，请及时保存`:'';
}

export async function mountLibrary(ctx,root,route,params={}) {
  let active=true;const accountVersion=ctx.accountVersion,timers=new Set(),dialogs=new Set(),objectUrls=new Set();
  const valid=()=>active&&ctx.accountVersion===accountVersion&&root.isConnected!==false;
  const media=value=>mediaUrl(value,root.ownerDocument.baseURI);
  const privateMedia=value=>{const url=media(value);if(!url)return '';const parsed=new URL(url);return parsed.protocol==='https:'&&parsed.origin!==new URL(root.ownerDocument.baseURI).origin?url:'';};
  const privateUrl=async(job,kind,force=false)=>{
    const field=kind==='orig'?'orig_url':'result_url',direct=privateMedia(job[field]);if(direct&&!force)return direct;
    const repaired=await ctx.api('/api/jobs/'+encode(job.id)+'/refresh-media?kind='+kind,{method:'POST',data:{}});if(!valid())return '';
    const url=privateMedia(repaired.url);if(!url)throw new Error('COS 图片直链尚未就绪，请稍后重试');job[field]=url;return url;
  };
  const el=(tag,cls,text)=>{const node=ctx.element?ctx.element(tag,cls,text):document.createElement(tag);if(cls)node.className=cls;if(text!=null)node.textContent=String(text);return node;};
  const button=(label,handler,cls='lib-button')=>{const b=el('button',cls,label);b.type='button';b.addEventListener('click',handler);return b;};
  const navigate=(name,values={})=>{if(valid())ctx.navigate(name,values);};
  const notify=text=>{if(valid())ctx.toast(String(text));};
  const cleanup=()=>{active=false;timers.forEach(clearTimeout);timers.clear();dialogs.forEach(d=>d.remove());dialogs.clear();objectUrls.forEach(URL.revokeObjectURL);objectUrls.clear();};
  root.replaceChildren();root.classList.add('web-library');root.dataset.route=route;
  const heading=(title,sub)=>{const h=el('header','lib-heading');h.append(el('p','lib-eyebrow','废片新生所 · 个人创作馆'),el('h1','',title),el('p','lib-subtitle',sub));root.append(h);return h;};
  const actions=()=>{const bar=el('div','lib-actions');return bar;};
  const link=(label,name,values={})=>button(label,()=>navigate(name,values),'lib-link');
  const state=(host,text,retry)=>{const box=el('div','lib-state');box.append(el('p','',text));if(retry)box.append(button('重新加载',retry,'lib-button lib-small'));host.append(box);return box;};
  const write=async(b,fn)=>{
    if(b.disabled||!valid())return;b.disabled=true;
    try{if(!await ctx.requireLogin())return;if(!valid()){ctx.toast('登录成功，请再次点击刚才的操作继续');return;}await fn();}
    catch(e){notify(e.message||'操作暂未完成，请重试');}
    finally{if(valid())b.disabled=false;}
  };
  const image=(url,label,cls='lib-image')=>{
    const safe=media(url);if(!safe)return el('div',cls+' lib-image-empty','图片暂不可用');
    const img=el('img',cls);img.src=safe;img.alt=label;img.loading='lazy';img.addEventListener('error',()=>{if(valid()){img.classList.add('lib-image-error');img.alt='图片读取失败，请刷新';}});return img;
  };
  const modal=(title,body)=>{
    if(!valid())return null;const dialog=el('dialog','lib-dialog'),content=el('section','lib-dialog-content'),head=el('header','lib-dialog-heading');
    head.append(el('h2','',title),button('关闭',()=>{dialog.close();dialog.remove();dialogs.delete(dialog);},'lib-dialog-close'));
    content.append(head,body);dialog.append(content);root.append(dialog);dialogs.add(dialog);dialog.addEventListener('click',e=>{if(e.target===dialog){dialog.close();dialog.remove();dialogs.delete(dialog);}});
    dialog.showModal();return dialog;
  };
  const preview=job=>{
    const body=el('div','lib-preview'),result=job.result_url||job.resultUrl,original=job.orig_url||job.origUrl;
    if(original){const before=el('figure');before.append(image(original,'修护前原片'),el('figcaption','','修护前原片'));body.append(before);}
    const after=el('figure');after.append(image(result,'生成结果'),el('figcaption','','生成结果'));body.append(after);modal(original?'原图与新生图':'查看作品',body);
  };
  const report=(kind,id)=>{
    const body=el('div','lib-report-options');REPORTS.forEach(([reason,label])=>{const b=button(label,()=>write(b,async()=>{
      await ctx.api(`/api/community/${kind==='post'?'posts':'comments'}/${encode(id)}/report`,{method:'POST',data:{reason}});if(!valid())return;
      notify('举报已收到，谢谢你的反馈');dialog.close();dialog.remove();dialogs.delete(dialog);
    }));body.append(b);});const dialog=modal('选择举报原因',body);
  };
  const download=async(job,b)=>write(b,async()=>{
    const fresh=await ctx.api('/api/jobs/'+encode(job.id));if(!valid())return;if(fresh.status!=='succeeded')throw new Error('作品尚未完成');
    let url=await privateUrl(fresh,'result');if(!valid())return;let response=await fetch(url,{credentials:'omit'});
    if([401,403].includes(response.status)){url=await privateUrl(fresh,'result',true);if(!valid())return;response=await fetch(url,{credentials:'omit'});}
    if(!response.ok)throw new Error('COS 图片下载未完成，请重试');const blob=await response.blob();if(!valid())return;
    if(!/^image\//.test(blob.type||''))throw new Error('下载响应不是图片，请重试');
    const objectUrl=URL.createObjectURL(blob);objectUrls.add(objectUrl);const a=el('a');a.href=objectUrl;a.download='新生作品-'+job.id+'.jpg';a.hidden=true;root.append(a);a.click();a.remove();notify('照片已下载，请查看浏览器下载记录');
    const timer=setTimeout(()=>{URL.revokeObjectURL(objectUrl);objectUrls.delete(objectUrl);timers.delete(timer);},1000);timers.add(timer);
  });
  const recreate=async(job,b)=>write(b,async()=>{
    const recipe=await ctx.api('/api/jobs/'+encode(job.id)+'/recipe');if(!valid())return;
    if(!recipe.complete)throw new Error('旧记录的参数不完整，请重新选择照片或填写描述');
    if(!['photo','text'].includes(recipe.input_mode))throw new Error('作品模式未完整记录，请重新开始创作');
    if(recipe.input_mode!=='text'&&!privateMedia(recipe.orig_url))throw new Error('原始照片暂未提供 COS 直链，请重新选择照片');
    navigate(recipe.input_mode==='text'?'text':'create',{recipe});
  });

  function pager(host,url,itemView,{empty='这里还没有记录',itemsKey='items',onData,limit=24}={}){
    const message=el('div','lib-page-message'),list=el('div','lib-record-list'),footer=el('div','lib-page-footer');host.append(message,list,footer);
    const p={rows:[],offset:0,hasMore:false,loading:false,sequence:0};
    p.reset=()=>{p.sequence++;p.loading=false;p.rows=[];p.offset=0;p.hasMore=false;p.draw();};
    p.draw=()=>{if(!valid())return;list.replaceChildren(...p.rows.map(itemView));footer.replaceChildren();if(p.hasMore){const more=button(p.loading?'正在加载…':'加载更多',()=>p.load(true));more.disabled=p.loading;more.dataset.action='load-more';footer.append(more);}};
    p.load=async(more=false)=>{
      if(!valid()||p.loading||(more&&!p.hasMore))return;p.loading=true;const sequence=++p.sequence;
      message.replaceChildren();if(!p.rows.length)state(message,'正在读取…');p.draw();
      try{
        const d=await ctx.api(url(more?p.offset:0,limit));if(!valid()||sequence!==p.sequence)return;
        const rows=d[itemsKey];if(!Array.isArray(rows))throw new Error('列表数据异常');
        const next=Number(d.next_offset);if(d.has_more&&(!Number.isInteger(next)||next<=(more?p.offset:0)))throw new Error('分页数据异常，请刷新');
        p.rows=[...new Map((more?p.rows.concat(rows):rows).map(x=>[String(x.id),x])).values()];p.offset=Number.isInteger(next)?next:rows.length;p.hasMore=!!d.has_more;
        message.replaceChildren();if(onData)onData(d);if(!p.rows.length)state(message,empty);p.loading=false;p.draw();root.dataset.state='loaded';
      }catch(e){if(valid()&&sequence===p.sequence){message.replaceChildren();state(message,e.message||'读取失败，请重试',()=>p.load(false));root.dataset.state='error';}}
      finally{if(valid()&&sequence===p.sequence){p.loading=false;p.draw();}}
    };
    p.list=list;return p;
  }

  const privateRoutes=new Set(['works','submissions','submit','my','credits','orders','invites']);
  if(privateRoutes.has(route)){
    let loggedIn=false;try{loggedIn=await ctx.requireLogin();}catch(e){notify(e.message||'请先注册并登录');}
    if(!valid())return cleanup;
    if(!loggedIn){heading('登录后查看你的创作馆','注册网页账号后即可使用；新账号初始余额为 0。');root.append(button('注册或登录',()=>ctx.requireLogin()));return cleanup;}
  }

  if(route==='works'){
    const head=heading('我的作品','原图与成品保留 30 天，请及时下载满意的作品。');
    const feedbackPending=new Set();
    let status=['all',...Object.keys(STATUS)].includes(params.status)?params.status:'all',pollTimer=null;const pollStarted=Date.now(),filters=actions(),summary=el('p','lib-meta');root.append(filters,summary);
    const toolbar=actions();const refresh=button('刷新记录',()=>p.load(false),'lib-link'),clear=button('清空个人作品',()=>write(clear,async()=>{
      if(!await ctx.confirm('删除全部个人作品？社区已发布副本需在“我的投稿”单独撤回。')||!valid())return;
      await ctx.api('/api/my/jobs',{method:'DELETE'});if(valid())await p.load(false);
    }),'lib-link lib-danger');toolbar.append(refresh,link('我的社区投稿','submissions'),clear);head.append(toolbar);
    const renderWork=job=>{
      const card=el('article','lib-card work-card');card.dataset.id=job.id;if(params.jobId===job.id)card.classList.add('lib-focused');
      const photo=button('',()=>write(photo,async()=>openWork(job)),'lib-photo-button');photo.append(image(privateMedia(job.result_url)||privateMedia(job.orig_url),'个人作品'));card.append(photo);
      const content=el('div','lib-card-body'),line=el('div','lib-card-title');line.append(el('h2','',job.template_name||'我的新生作品'),el('span','lib-badge',STATUS[job.status]||job.status));content.append(line,el('p','lib-meta',dateLabel(job.created_at)));
      if(job.status==='processing')content.append(el('p','lib-progress',({queued:'任务排队中',normalize:'正在准备照片',enhance:'正在生成图片',upscale:'正在精细处理',store_cos:'正在保存图片',finalize:'正在整理结果'})[job.stage]||'任务在后台处理中'));
      if(job.status==='failed'){
        content.append(el('p','lib-error',job.error||'生成失败'),el('p','lib-meta',settlement(job)),link('核对光子明细','credits'));
        const violation=job.violation;
        if(violation){
          const box=el('section','lib-violation');box.append(el('h3','','内容审核记录'),el('p','lib-error',violation.message||'审核原因未完整记录'));
          box.append(el('p','lib-meta',Number.isInteger(violation.charged)?`内容审核单独扣除 ${violation.charged} 光子（与生成预扣及退回分开）`:'审核扣点金额未完整记录，不按当前价格推算'));
          box.append(el('p','lib-meta',Number.isInteger(violation.weekly_count)?`近 7 天累计 ${violation.weekly_count} 次`:'近 7 天次数未完整记录'),el('p','lib-meta',typeof violation.banned==='boolean'?(violation.banned?'账号状态：已封禁':'账号状态：未封禁'):'账号状态未完整记录'));
          if(violation.violation_id){const feedback=button(violation.feedback_submitted?'反馈已提交，等待复核':'提交误判反馈',()=>write(feedback,async()=>openFeedback(job)),'lib-button lib-small lib-secondary');feedback.dataset.action='violation-feedback';feedback.disabled=!!violation.feedback_submitted||feedbackPending.has(violation.violation_id);box.append(feedback);}
          else box.append(el('p','lib-meta','旧任务缺少复核凭据，审核明细未完整记录'));
          content.append(box);
        }
      }
      const left=expiry(job);if(left)content.append(el('p','lib-expiry',left));const bar=actions();
      if(job.status==='succeeded'){
        const save=button('下载照片',()=>download(job,save),'lib-button lib-small');save.disabled=!job.result_url;save.dataset.action='download';
        const compare=button(job.orig_url?'查看对比':'放大查看',()=>write(compare,async()=>openWork(job)),'lib-button lib-small lib-secondary');
        const same=button('沿用参数再创作',()=>recreate(job,same),'lib-link');bar.append(save,compare,same,link('投稿到社区','submit',{jobId:job.id}));
      }
      const remove=button('删除',()=>write(remove,async()=>{
        if(!await ctx.confirm('删除这件个人作品？社区独立副本不会一起撤回。')||!valid())return;
        await ctx.api('/api/my/jobs/'+encode(job.id),{method:'DELETE'});if(valid()){p.rows=p.rows.filter(x=>x.id!==job.id);p.draw();notify('作品已删除');await p.load(false);}
      }),'lib-link lib-danger');remove.dataset.action='delete-work';bar.append(remove);content.append(bar);card.append(content);return card;
    };
    function openFeedback(job){
      const violation=job.violation,id=violation&&violation.violation_id;if(!id||violation.feedback_submitted)return;
      if(feedbackPending.has(id)){notify('反馈正在提交，请等待核对结果');return;}
      const form=el('form','lib-composer'),input=el('textarea','lib-input'),send=el('button','lib-button','提交复核'),error=el('p','lib-error');input.rows=5;input.maxLength=500;input.setAttribute('aria-label','误判反馈内容');input.placeholder='请描述需要人工复核的情况（1–500 字）';send.type='submit';send.dataset.action='send-violation-feedback';error.setAttribute('role','status');
      form.append(el('p','lib-meta','反馈提交后由后台人工复核；本页不会自动改动账号状态或光子账务。'),input,error,send);const dialog=modal('提交误判反馈',form);
      form.addEventListener('submit',e=>{e.preventDefault();write(send,async()=>{
        const text=input.value.trim();if(!text||text.length>500)throw new Error('反馈内容需为 1–500 字');if(feedbackPending.has(id))return;
        feedbackPending.add(id);input.disabled=true;error.textContent='';
        try{const result=await ctx.api('/api/me/violation-feedback',{method:'POST',data:{violation_id:id,message:text}});if(!valid())return;if(!result||result.ok!==true)throw new Error('反馈结果暂未确认，请刷新作品记录核对');
          violation.feedback_submitted=true;notify(result.message||'反馈已提交，等待人工复核');dialog.close();dialog.remove();dialogs.delete(dialog);p.draw();
        }catch(err){if(valid()){error.textContent=err.message||'反馈暂未提交，请重试';throw err;}}
        finally{feedbackPending.delete(id);if(valid())input.disabled=false;}
      });});
    }
    async function openWork(job){const fresh=await ctx.api('/api/jobs/'+encode(job.id));if(!valid())return;if(fresh.status!=='succeeded')throw new Error(fresh.status==='failed'?fresh.error||'生成失败':'作品仍在生成，请稍候');fresh.result_url=await privateUrl(fresh,'result');if(!valid())return;if(fresh.orig_url)fresh.orig_url=await privateUrl(fresh,'orig');if(valid())preview(fresh);}
    const p=pager(root,(offset,limit)=>`/api/my/jobs?limit=${limit}&offset=${offset}&status=${status}`,renderWork,{itemsKey:'jobs',empty:'当前筛选下暂无作品',onData:d=>{
      summary.textContent=`当前筛选共 ${d.total??p.rows.length} 件作品`;renderFilters(d.status_counts||{});if(p.rows.some(j=>j.status==='processing'))schedule();
    }});p.list.classList.add('lib-work-grid');
    function renderFilters(counts={}){filters.replaceChildren(...[['all','全部'],...Object.entries(STATUS)].map(([id,label])=>{const b=button(label+(Number.isInteger(counts[id])?' '+counts[id]:''),()=>{if(id===status)return;status=id;params.jobId='';p.reset();renderFilters(counts);p.load(false);},'lib-filter'+(status===id?' lib-filter-active':''));b.dataset.action='status-'+id;return b;}));}
    renderFilters();let detailCursor=0;
    const poll=async()=>{
      if(!valid())return;
      const sequence=p.sequence,selectedStatus=status;
      try{
        const d=await ctx.api('/api/my/jobs?limit=24&offset=0&status=processing');if(!valid())return;if(sequence!==p.sequence||status!==selectedStatus){if(p.rows.some(j=>j.status==='processing'))schedule();return;}
        const updates=new Map((d.jobs||[]).map(j=>[j.id,j])),missing=p.rows.filter(j=>j.status==='processing'&&!updates.has(j.id));
        const start=missing.length?detailCursor%missing.length:0,batch=missing.slice(start).concat(missing.slice(0,start)).slice(0,6);detailCursor=start+batch.length;
        const final=await Promise.all(batch.map(async j=>{try{return await ctx.api('/api/jobs/'+encode(j.id));}catch(e){return null;}}));if(!valid())return;if(sequence!==p.sequence||status!==selectedStatus){if(p.rows.some(j=>j.status==='processing'))schedule();return;}
        final.filter(Boolean).forEach(j=>updates.set(j.id,j));p.rows=p.rows.map(j=>updates.get(j.id)||j).filter(j=>status==='all'||j.status===status);p.draw();renderFilters(d.status_counts||{});
      }catch(e){/* Keep known cards; the refresh action exposes network errors. */}
      if(valid()&&p.rows.some(j=>j.status==='processing'))schedule();
    };
    const schedule=()=>{if(pollTimer!==null||!valid())return;const timer=setTimeout(()=>{timers.delete(timer);pollTimer=null;poll();},Date.now()-pollStarted<60000?1500:5000);pollTimer=timer;timers.add(timer);};
    p.load(false).then(async()=>{
      if(!valid())return;
      if(params.jobId&&!p.rows.some(j=>j.id===params.jobId))try{const job=await ctx.api('/api/jobs/'+encode(params.jobId));if(valid()){p.rows.unshift(job);p.draw();}}catch(e){notify(e.message||'作品已到期或不存在');}
      if(valid()&&p.rows.some(j=>j.status==='processing'))schedule();
    });
  } else if(route==='community'){
    const head=heading('灵感沙龙','发现喜欢的效果，保存灵感，再试试同款风格。');const toolbar=actions();toolbar.append(link('投稿我的作品','works'),link('我的投稿','submissions'));head.append(toolbar);
    let category='all',liked=false;const filters=actions();root.append(filters);
    const renderPost=post=>{
      const card=el('article','lib-card community-card');card.dataset.id=post.id;
      const photo=button('',()=>navigate('post',{id:post.id}),'lib-photo-button');photo.append(image(post.resultUrl,post.title||'社区作品'));card.append(photo);
      const content=el('div','lib-card-body');content.append(el('p','lib-meta',`${post.authorName||'新生创作者'} · ${post.categoryName||''}`),el('h2','',post.title),el('p','lib-story',post.story||''));if(post.featured)content.append(el('span','lib-badge','精选展品'));
      const bar=actions(),like=button((post.liked?'♥ 已喜欢':'♡ 喜欢')+' '+(post.likes||0),()=>write(like,async()=>{const r=await ctx.api('/api/community/posts/'+encode(post.id)+'/like',{method:'PUT',data:{liked:!post.liked}});if(valid()){Object.assign(post,r);p.draw();if(liked)await p.load(false);}}),'lib-link');like.dataset.action='like-post';
      bar.append(like,link('留言 '+(post.comments||0),'post',{id:post.id}));if(post.templateId)bar.append(link('做同款风格 →','create',{templateId:post.templateId}));content.append(bar);card.append(content);return card;
    };
    const p=pager(root,(offset,limit)=>`/api/community?limit=${limit}&offset=${offset}&category=${category}`+(liked?'&liked_only=true':''),renderPost,{empty:'当前展区暂无作品'});p.list.classList.add('lib-work-grid');
    function drawFilters(){filters.replaceChildren(...CATEGORIES.map(([id,label])=>button(label,()=>{category=id;liked=false;p.reset();drawFilters();p.load(false);},'lib-filter'+(!liked&&category===id?' lib-filter-active':''))));
      const favorites=button('我喜欢的',()=>write(favorites,async()=>{liked=true;category='all';p.reset();drawFilters();await p.load(false);}),'lib-filter'+(liked?' lib-filter-active':''));favorites.dataset.action='liked-filter';filters.append(favorites);}
    drawFilters();p.load(false);
  } else if(route==='post'){
    heading('作品与留言','公开作品经作者授权展示；留言经审核后发布。');const details=el('div'),commentsHost=el('section','lib-comments');root.append(details,commentsHost);
    let post=null,ticket=params.commentTicket||null;const draft=el('textarea','lib-input');draft.maxLength=500;draft.rows=3;draft.value=typeof params.commentDraft==='string'?params.commentDraft.slice(0,500):'';draft.placeholder='聊聊你喜欢的色彩与创作灵感…';draft.setAttribute('aria-label','作品留言');
    draft.addEventListener('input',()=>{params.commentDraft=draft.value;});
    const composer=el('form','lib-composer'),send=el('button','lib-button','发布留言');send.type='submit';composer.append(draft,send);
    const cp=pager(commentsHost,(offset,limit)=>`/api/community/posts/${encode(params.id)}/comments?limit=${limit}&offset=${offset}`,row=>{
      const card=el('article','lib-card lib-comment');card.dataset.id=row.id;card.append(el('p','lib-meta',`${row.author_name} · ${dateLabel(row.created_at)}`),el('p','lib-comment-text',row.content));
      const bar=actions(),like=button((row.liked?'♥':'♡')+' '+row.likes,()=>write(like,async()=>{const r=await ctx.api('/api/community/comments/'+encode(row.id)+'/like',{method:'PUT',data:{liked:!row.liked}});if(valid()){Object.assign(row,r);cp.draw();}}),'lib-link');bar.append(like);
      if(row.mine){bar.append(button('复制到留言框',()=>{draft.value=row.content;draft.focus();},'lib-link'));const remove=button('删除',()=>write(remove,async()=>{if(!await ctx.confirm('删除这条留言？')||!valid())return;await ctx.api('/api/community/comments/'+encode(row.id),{method:'DELETE'});if(valid())await cp.load(false);}),'lib-link lib-danger');remove.dataset.action='delete-comment';bar.append(remove);}
      const flag=button('举报',()=>write(flag,async()=>report('comment',row.id)),'lib-link');bar.append(flag);card.append(bar);return card;
    },{empty:'还没有留言，留下第一点创作灵感吧。',limit:30});
    commentsHost.prepend(el('h2','','作品留言'),composer);
    composer.addEventListener('submit',e=>{e.preventDefault();params.commentDraft=draft.value;write(send,async()=>{
      const content=draft.value.trim();if(!content)throw new Error('请写下留言');if(!ticket||ticket.content!==content)ticket={content,id:'comment_'+Date.now().toString(36)+'_'+Math.random().toString(36).slice(2,12)};
      params.commentTicket=ticket;draft.disabled=true;
      try{await ctx.api('/api/community/posts/'+encode(params.id)+'/comments',{method:'POST',data:{content,request_id:ticket.id}});if(!valid())return;
        ticket=null;params.commentTicket=null;if(draft.value.trim()===content){draft.value='';params.commentDraft='';}else params.commentDraft=draft.value;
        notify('留言已通过审核并发布');await cp.load(false);
      }finally{if(valid())draft.disabled=false;}
    });});
    const loadPost=async()=>{
      details.replaceChildren();state(details,'正在展开作品…');try{
        const d=await ctx.api('/api/community/posts/'+encode(params.id));if(!valid())return;post=d.post;details.replaceChildren();commentsHost.hidden=false;
        const card=el('article','lib-card lib-post-detail'),photo=button('',()=>preview(post),'lib-photo-button');photo.append(image(post.resultUrl,post.title));card.append(photo);
        const body=el('div','lib-card-body');body.append(el('p','lib-meta',`${post.authorName} · ${post.date}`),el('h2','',post.title),el('p','lib-story',post.story||''));if(post.origUrl)body.append(button('查看修护前原片与对比',()=>preview(post),'lib-link'));
        const bar=actions(),like=button((post.liked?'♥ 已喜欢':'♡ 喜欢')+' '+post.likes,()=>write(like,async()=>{const r=await ctx.api('/api/community/posts/'+encode(post.id)+'/like',{method:'PUT',data:{liked:!post.liked}});if(valid()){Object.assign(post,r);like.textContent=(r.liked?'♥ 已喜欢':'♡ 喜欢')+' '+r.likes;}}),'lib-link');bar.append(like);
        if(post.templateId)bar.append(link('做同款风格 →','create',{templateId:post.templateId}));const flag=button('举报作品',()=>write(flag,async()=>report('post',post.id)),'lib-link');bar.append(flag);body.append(bar);card.append(body);details.append(card);cp.load(false);root.dataset.state='loaded';
      }catch(e){if(valid()){details.replaceChildren();state(details,e.message||'作品已下架或不存在',e.status===404?null:loadPost);commentsHost.hidden=true;root.dataset.state='error';}}
    };loadPost();
  } else if(route==='submissions'){
    const head=heading('我的社区投稿','你决定公开哪些图片；审核通过后才会展示。');head.append(link('选择作品投稿','works'));
    const p=pager(root,(offset,limit)=>`/api/community/submissions/mine?limit=${limit}&offset=${offset}`,row=>{
      const card=el('article','lib-card lib-submission');card.dataset.id=row.id;const body=el('div','lib-card-body'),title=el('div','lib-card-title');title.append(el('h2','',row.title),el('span','lib-badge',SUBMISSIONS[row.status]||row.status));body.append(title,el('p','lib-meta',`${dateLabel(row.submitted_at)} · ${row.share_original?'成品与原图公开':'仅公开成品'}`));
      if(row.reason)body.append(el('p','lib-error',row.reason));if(row.rewarded)body.append(el('p','lib-gold',`精选奖励 ${row.reward} 光子${row.reward>0?'已到账':'已登记'} · 每件作品仅一次`));
      const bar=actions();if(row.status==='published')bar.append(link('查看我的帖子','post',{id:row.id}));
      if(['uploading','rejected','withdrawn'].includes(row.status))bar.append(link(row.status==='uploading'?'继续提交':'修改后再投稿','submit',{jobId:row.job_id,preset:row}));
      if(row.status!=='withdrawn'){const withdraw=button('撤回投稿',()=>write(withdraw,async()=>{
        if(!await ctx.confirm('撤回后社区停止展示并清理独立图片副本；他人已下载的副本不随之收回。精选奖励不重复发放。')||!valid())return;
        await ctx.api('/api/community/submissions/'+encode(row.id)+'/withdraw',{method:'POST',data:{revision:row.revision}});if(valid())await p.load(false);
      }),'lib-link lib-danger');withdraw.dataset.action='withdraw';bar.append(withdraw);}body.append(bar);card.append(body);return card;
    },{empty:'还没有投稿，从已完成的作品中选一件吧。',limit:30});p.load(false);
  } else if(route==='submit'){
    heading('把新生之作，留在展厅','原图默认不公开；本次授权需要你重新确认。');const host=el('section','lib-form-card');root.append(host);const preset=params.preset||{};
    const load=async()=>{
      host.replaceChildren();state(host,'正在读取个人作品…');try{
        if(!params.jobId){host.replaceChildren();state(host,'请先从已完成的个人作品中选择一件投稿');host.append(link('选择作品','works'));return;}
        const job=await ctx.api('/api/jobs/'+encode(params.jobId));if(!valid())return;
        if(job.status!=='succeeded'||!job.result_url||job.expires_at&&job.expires_at*1000<=Date.now())throw new Error('请选择已完成且未过期的作品');job.result_url=await privateUrl(job,'result');if(!valid())return;if(job.orig_url)try{job.orig_url=await privateUrl(job,'orig');}catch(e){job.orig_url='';}if(!valid())return;host.replaceChildren();host.append(image(job.result_url,'本次投稿的成品'));
        const form=el('form','lib-submit-form'),title=el('input','lib-input'),story=el('textarea','lib-input'),category=el('select','lib-input');title.maxLength=60;title.required=true;title.value=preset.title||job.template_name||'我的新生作品';title.setAttribute('aria-label','作品标题');story.maxLength=500;story.rows=5;story.value=preset.story||'';story.setAttribute('aria-label','创作故事');
        CATEGORIES.forEach(([id,name])=>{const option=el('option','',id==='all'?'其它':name);option.value=id;category.append(option);});category.value=preset.category||'all';category.setAttribute('aria-label','投稿展区');
        const share=el('input');share.type='checkbox';share.checked=!!(preset.share_original&&job.orig_url);share.disabled=!job.orig_url;share.dataset.action='share-original';const shareLabel=el('label','lib-checkbox');shareLabel.append(share,el('span','','同时公开修护前原图（默认关闭）'));
        const before=el('div'),summary=el('p','lib-consent-summary'),consent=el('input');consent.type='checkbox';consent.dataset.action='consent';const consentLabel=el('label','lib-checkbox');consentLabel.append(consent,el('span','','我确认有权公开所选图片与文字，并同意审核通过后在社区展示和被访客分享。'));
        const renderPrivacy=()=>{before.replaceChildren();if(share.checked&&job.orig_url)before.append(image(job.orig_url,'本次公开的修护前原图'));summary.textContent=share.checked?'本次公开：成品 + 修护前原图':'本次公开：仅成品，不公开原图';consent.checked=false;};share.addEventListener('change',renderPrivacy);renderPrivacy();
        const submit=el('button','lib-button','提交审核');submit.type='submit';submit.dataset.action='submit-post';const error=el('p','lib-error');error.setAttribute('role','status');
        form.append(el('label','lib-field-label','作品标题'),title,el('label','lib-field-label','创作故事（选填）'),story,el('label','lib-field-label','展区'),category,shareLabel,before,summary,
          el('p','lib-meta','社区图片副本独立保存；个人作品到期不影响已发布帖子。未通过或超过 7 天未审核的图片会清理。'),consentLabel,error,submit);host.append(form);
        form.addEventListener('submit',e=>{e.preventDefault();write(submit,async()=>{
          if(!consent.checked)throw new Error('请先确认本次公开展示授权');if(!title.value.trim())throw new Error('请填写作品标题');error.textContent='';
          [title,story,category,share,consent].forEach(field=>{field.disabled=true;});
          try{const r=await ctx.api('/api/community/submissions',{method:'POST',data:{job_id:job.id,title:title.value.trim(),story:story.value.trim(),category:category.value,share_original:share.checked,consent:true}});if(!valid())return;consent.checked=false;notify(r.submission.status==='published'?'该作品已发布':'投稿已登记，审核后展示');navigate('submissions');}
          catch(e){if(valid()){error.textContent=(e.message||'提交暂未完成')+'；同一作品不会重复登记，可到“我的投稿”核对状态。';throw e;}
          }finally{if(valid()){[title,story,category,consent].forEach(field=>{field.disabled=false;});share.disabled=!job.orig_url;}}
        });});host.append(link('查看我的投稿','submissions'));root.dataset.state='loaded';
      }catch(e){if(valid()){host.replaceChildren();state(host,e.message||'作品读取失败',load);root.dataset.state='error';}}
    };load();
  } else if(route==='my'){
    heading('我的创作档案','网站注册账号初始为 0 光子；余额由后台手动增加。');const host=el('div');root.append(host);
    const load=async()=>{host.replaceChildren();state(host,'正在同步账号…');try{
      const user=await ctx.api('/api/me');if(!valid())return;if(Number.isFinite(user.balance))ctx.setBalance(user.balance);
      const card=el('section','lib-card lib-card-body'),sessionUser=ctx.state.auth&&ctx.state.auth.user||ctx.state.user||{};
      const bound=!!(user.wechat_bound||sessionUser.wechat_bound),bindingReady=!!(ctx.state.auth&&ctx.state.auth.wechat_login&&ctx.state.auth.wechat_login.ready);
      card.append(el('h2','',user.nickname||'新生创作者'),el('p','lib-meta','账号编号 '+(user.user_id||'—')),el('p','lib-meta','网页注册身份：'+(user.username||sessionUser.username||user.mobile_masked||user.phone_masked||'已注册')),el('p','lib-meta',bound?'微信账号已绑定 · 作品和账务使用同一账号':'微信账号尚未绑定'));
      if(!bound&&!bindingReady)card.append(el('p','lib-meta','微信绑定接入尚未配置，配置完成后才能绑定；当前不会自动合并其它账号。'));
      const bind=button(bound?'微信账号已绑定':bindingReady?'绑定微信账号':'微信绑定尚未开通（查看说明）',()=>write(bind,async()=>{if(typeof ctx.startWechatLink!=='function')throw new Error('微信账号接入尚未配置，配置完成后可在这里绑定');await ctx.startWechatLink();}),'lib-button lib-secondary');bind.dataset.action='wechat-link';bind.dataset.ready=String(bindingReady);bind.disabled=bound;card.append(bind);
      const form=el('form','lib-profile-form'),input=el('input','lib-input'),save=el('button','lib-button','保存昵称');input.maxLength=24;input.value=user.nickname||'';input.placeholder='设置展示昵称';input.setAttribute('aria-label','展示昵称');save.type='submit';form.append(input,save);
      form.addEventListener('submit',e=>{e.preventDefault();write(save,async()=>{const nickname=input.value.trim();if(!nickname)throw new Error('请输入昵称');const r=await ctx.api('/api/me/profile',{method:'POST',data:{nickname}});if(!valid())return;if(ctx.state.user)ctx.state.user.nickname=r.nickname;card.querySelector('h2').textContent=r.nickname;notify('昵称已保存');});});card.append(form);
      const accountActions=actions();if(typeof ctx.changePassword==='function'){const change=button('修改密码',()=>write(change,async()=>ctx.changePassword()),'lib-link');accountActions.append(change);}if(typeof ctx.logout==='function'){const logout=button('退出登录',()=>write(logout,async()=>ctx.logout()),'lib-link lib-danger');accountActions.append(logout);}card.append(accountActions);
      const balance=el('section','lib-balance');balance.append(el('p','','可用光子'),el('strong','',Number.isFinite(user.balance)?'✦ '+user.balance:'—'),el('p','','需要增加余额，请联系管理员在后台手动补给。'));const nav=el('nav','lib-account-links');[['我的作品','works'],['我的投稿','submissions'],['光子明细','credits'],['已有充值订单','orders'],['历史邀请记录','invites']].forEach(([label,r])=>nav.append(link(label+' ›',r)));
      host.replaceChildren(card,balance,nav);root.dataset.state='loaded';
    }catch(e){if(valid()){host.replaceChildren();state(host,e.message||'账号读取失败',load);root.dataset.state='error';}}};load();
  } else if(route==='credits'){
    const head=heading('光子明细','仅展示服务端记录的真实收支；增加余额请联系管理员在后台操作。');head.append(link('查看已有充值订单','orders'));
    const p=pager(root,(offset,limit)=>`/api/me/credits?limit=${limit}&offset=${offset}`,row=>{
      const card=el('article','lib-card lib-ledger-row'),body=el('div');body.append(el('h2','',row.title),el('p','lib-meta',dateLabel(row.created_at)));if(row.order_id)body.append(link('查看关联订单 ›','orders',{id:row.order_id}));else if(row.job_id)body.append(link('查看关联作品 ›','works',{jobId:row.job_id}));
      card.append(body,el('strong',row.amount<0?'lib-amount-spend':'lib-amount',`${row.amount>0?'+':''}${row.amount} 光子`));return card;
    },{empty:'暂无已记录的光子明细',limit:30});p.load(false);
  } else if(route==='orders'){
    const head=heading('已有充值订单','这里只展示共享账号已有订单，不提供网页购买入口。');
    const p=pager(root,(offset,limit)=>`/api/payment/orders?limit=${limit}&offset=${offset}`,row=>{
      const card=el('article','lib-card lib-card-body lib-order');card.dataset.id=row.id;if(row.id===params.id)card.classList.add('lib-focused');const title=el('div','lib-card-title');title.append(el('h2','',row.points+' 光子'),el('span','lib-badge',ORDER_STATUS[row.status]||'核对中'));
      card.append(title,el('p','lib-meta',`¥${(Number(row.amount)/100).toFixed(2)} · ${dateLabel(row.created_at)}`),el('p','lib-order-id','订单号 '+row.id));if(row.refunded_fen)card.append(el('p','lib-meta',`已退款 ¥${(row.refunded_fen/100).toFixed(2)} · 回收 ${row.reversed_points} 光子`));
      const check=button('核对到账状态',()=>write(check,async()=>{const r=await ctx.api('/api/payment/orders/'+encode(row.id)+'/sync',{method:'POST'});if(!valid())return;if(Number.isFinite(r.balance))ctx.setBalance(r.balance);notify('已核对最新订单状态，不会再次扣款');await p.load(false);if(valid()&&r.order&&!p.rows.some(o=>o.id===r.order.id)){p.rows.unshift(r.order);p.draw();}}),'lib-button lib-small lib-secondary');check.dataset.action='sync-order';if(!['refunded','closed'].includes(row.status))card.append(check);return card;
    },{empty:'暂无已有订单',limit:30,onData:d=>{if(Number.isFinite(d.balance))ctx.setBalance(d.balance);}});
    head.append(button('刷新订单',()=>p.load(false),'lib-link'));p.load(false).then(async()=>{
      if(!valid()||!params.id||p.rows.some(o=>o.id===params.id))return;
      try{const selected=await ctx.api('/api/payment/orders/'+encode(params.id));if(valid()&&selected.order){p.rows.unshift(selected.order);p.draw();}}
      catch(e){notify(e.message||'订单不存在');}
    });
  } else if(route==='invites'){
    heading('历史邀请记录','展示共享账号已有邀请与到账记录；网站不提供绑定邀请码或邀请奖励。');const summary=el('p','lib-meta');root.append(summary);
    const p=pager(root,(offset,limit)=>`/api/me/invites?limit=${limit}&offset=${offset}`,row=>{
      const card=el('article','lib-card lib-ledger-row'),body=el('div');body.append(el('h2','',row.nickname||'新生创作者'),el('p','lib-meta',dateLabel(row.bound_at)));card.append(body,el('strong','lib-amount',row.reward==null?'历史金额未记录':'+'+row.reward+' 光子'));return card;
    },{empty:'暂无历史邀请记录',limit:30,onData:d=>{summary.textContent=`成功邀请 ${d.total||0} 位 · 已记录奖励 ${d.recorded_reward||0} 光子`+(d.historical_unknown?' · 历史缺失金额不按现价推算':'');}});p.load(false);
  } else {heading('创作馆','请选择个人作品或灵感沙龙。');root.append(link('查看作品','works'),link('浏览社区','community'));}
  return cleanup;
}
