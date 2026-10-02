"""Consent-bound community publication; no client-selected media keys or rewards."""
import time
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, StrictBool, Field
import cos_store as cos
from community_store import CommunityStore, CATEGORIES, editorial_thumbnail
from community_submissions import store_for, PENDING_TTL
from community_interactions import interactions_for, REPORT_REASONS
import wechat_sec
from account_links import owns, aliases_unlocked, marks
from tencent_cs import moderate_text, ModerationError


def catalog_context(m):
    """Public taxonomy follows the currently enabled, visible template catalog."""
    templates = getattr(m,'templates',None)
    groups = templates.list_groups(enabled_only=True) if templates and hasattr(templates,'list_groups') else []
    categories = [{'id':g['id'],'name':g['name']} for g in groups if g.get('id') != 'all']
    names = {g['id']:g['name'] for g in categories}
    rows = templates.list_templates(enabled_only=True) if templates and hasattr(templates,'list_templates') else []
    mapping = {t['id']:t['group_id'] for t in rows if t.get('group_id') in names}
    if templates and hasattr(templates,'list_groups'):
        # Existing administrative CommunityStore(settings) objects see current
        # groups too; this provider is not persisted or exposed in settings JSON.
        m.settings._community_category_provider = lambda: {
            g['id']:g['name'] for g in m.templates.list_groups(enabled_only=True) if g.get('id') != 'all'}
    return {'categories':categories,'names':names,'template_groups':mapping}


def category_fields(m, payload, context=None, legacy=False):
    context = context or catalog_context(m)
    raw = payload.get('category','all')
    if not isinstance(raw,str):raw='all'
    template_id = payload.get('template_id')
    group = raw if raw in context['names'] else context['template_groups'].get(template_id if isinstance(template_id,str) else '', 'all')
    name = context['names'].get(group,'未分组')
    return {'category':raw if legacy else group,
            'categoryName':CATEGORIES.get(raw,payload.get('category_name') or name) if legacy else name,
            'group_id':group,'group_name':name,'legacyCategory':raw}


class SubmissionBody(BaseModel):
    job_id: str = Field(min_length=6,max_length=32)
    title: str = Field(min_length=1,max_length=60)
    story: str = Field(default='',max_length=500)
    category: str = 'all'
    share_original: StrictBool = False
    consent: StrictBool = False


class RevisionBody(BaseModel):
    revision: int = Field(ge=1)


class LikeBody(BaseModel):
    liked: StrictBool


class CommentBody(BaseModel):
    content: str = Field(min_length=1, max_length=500)
    request_id: str = Field(pattern=r'^[A-Za-z0-9_-]{8,64}$')


class ReportBody(BaseModel):
    reason: str = Field(max_length=30)


def viewer_id(m, request):
    if request.headers.get('authorization') or getattr(request,'cookies',{}).get('site_session'):
        try:return m._current_user(request)['openid']
        except HTTPException:pass
    return None


def visible_post(m, sid):
    row=store_for(m.users).get(sid)
    if row:
        author=m.users.get_user(row['owner'])
        if row['status']!='published' or not author or author.get('banned') or not m.settings.cos_ready():
            raise HTTPException(404,detail='帖子已下架或不存在')
        return row
    row=next((p for p in CommunityStore(m.settings).public_items() if p['id']==sid),None)
    if not row:raise HTTPException(404,detail='帖子已下架或不存在')
    return row


def visible_comment(m, cid):
    row=interactions_for(m.users).get(cid)
    author=m.users.get_user(row['owner']) if row else None
    if not row or row['status']!='published' or not author or author.get('banned'):
        raise HTTPException(404,detail='评论已删除或不存在')
    visible_post(m,row['post_id'])
    return row


def admin_posts(m, status='all', q='', offset=0, limit=30):
    context = catalog_context(m)
    result=CommunityStore(m.settings).list(status,q,0,200)
    all_submissions=store_for(m.users).list(status='published',limit=200)[0]
    stats=result['stats'];stats['total']+=len(all_submissions);stats['published']+=len(all_submissions)
    stats['pinned']+=sum(bool(r['featured']) for r in all_submissions)
    items=[dict(p,source='editorial') for p in result['items']]
    if status!='paused':
        for row in all_submissions:
            p=row['payload']
            if q.strip().lower() not in ' '.join(str(p.get(k,'')) for k in ('title','story','author_name','template_name')).lower():continue
            items.append(dict(owner_view(m,row,context),source='submission',status='published',
                              category_name=category_fields(m,p,context)['categoryName'],template_name=p['template_name'],
                              pinned=bool(row['featured']),sort=100,created_at=row['submitted'],updated_at=row['updated']))
    items.sort(key=lambda p:(not p.get('pinned'),p.get('sort',100),-p.get('created_at',0),p['id']))
    offset=max(0,offset);limit=max(1,min(200,limit))
    return {'items':items[offset:offset+limit],'total':len(items),'stats':stats}


def admin_delete_post(m, sid):
    s=store_for(m.users)
    with s.flow_lock:
        row=s.get(sid)
        if not row:return CommunityStore(m.settings).delete(sid)
        s.withdraw(sid,row['owner'],row['revision']);enqueue_removal(m,row)
    m.cleanup.delete_cos_now(m.settings,keys(row))
    return {'ok':True,'affected':1}


def keys(row):
    return [row['payload'][k] for k in ('result_key','orig_key') if row['payload'].get(k)]


def signed(m, key, *, thumbnail=False):
    if not key or not m.settings.cos_ready():return ''
    if thumbnail:return cos.thumbnail_url(m.settings,key,ttl_seconds=300)
    return cos.presign(m.settings,'get',key,ttl_seconds=300)


def owner_view(m, row, context=None):
    p=row['payload'];visible=row['status'] in ('pending','published')
    fields = category_fields(m,p,context)
    return {'id':row['id'],'job_id':row['job_id'],'revision':row['revision'],'status':row['status'],
            'title':p['title'],'story':p['story'],'category':p['category'],'author_name':p['author_name'],
            **{key:fields[key] for key in ('categoryName','group_id','group_name','legacyCategory')},
            'share_original':p['share_original'],'reason':row['reason'],'featured':bool(row['featured']),
            'reward':row['reward'],'rewarded':store_for(m.users).rewarded(row['id']),'submitted_at':row['submitted'],
            'result_url':signed(m,p['result_key']) if visible else '',
            'thumbnail_url':signed(m,p['result_key'],thumbnail=True) if row['status']=='published' else '',
            'orig_url':signed(m,p['orig_key']) if visible and p['share_original'] else ''}


def enqueue_removal(m,row):
    for key in keys(row):m.cleanup.schedule('cos',key,time.time())


def maintain(m):
    s=store_for(m.users)
    with s.flow_lock:
        s.maintenance()
        # Repair a cleanup-queue cancellation interrupted after publication committed.
        for row in s.list(status='published',limit=200)[0]:
            for key in keys(row):m.cleanup.cancel('cos',key)


def community_user(m,request):
    user=m._current_user(request)
    if user.get('banned'):raise HTTPException(403,detail='账号已被封禁，暂不能进行社区操作')
    return user


def make_community_router(runtime):
    router=APIRouter()
    catalog_context(runtime())

    @router.get('/api/community/media/{filename}/thumbnail')
    def editorial_media_thumbnail(filename:str):
        try:data=CommunityStore(runtime().settings).thumbnail_bytes(filename)
        except (OSError,ValueError):raise HTTPException(404,detail='图片不存在')
        # Browser/proxy caches must not extend a paused publication's visibility.
        return Response(data,media_type='image/jpeg',headers={'Cache-Control':'private, no-store'})

    @router.post('/api/community/submissions')
    def submit(body:SubmissionBody,request:Request):
        m=runtime();user=community_user(m,request)
        if not body.consent:raise HTTPException(400,detail='请先确认社区公开展示授权')
        if m.settings.maintenance().get('enabled'):raise HTTPException(503,detail='服务维护中，请稍后投稿')
        title=body.title.strip();story=body.story.strip()
        if not title:raise HTTPException(400,detail='请填写标题并选择有效分类')
        job=m.jobs.get(m._safe_job_id(body.job_id))
        if not job or not owns(m.users,job.get('openid'),user['openid']) or job.get('deleted_at'):
            raise HTTPException(404,detail='作品不存在')
        context = catalog_context(m)
        if body.category not in CATEGORIES and body.category not in context['names']:
            previous = store_for(m.users).category_for_job(user['openid'],job['id'])
            if previous != body.category:raise HTTPException(400,detail='请选择有效分类；已停用分类可改为自动分组')
        if job.get('status')!='succeeded' or time.time()>=m._media_expires_at(job,'result'):
            raise HTTPException(409,detail='只能投稿已完成且未过期的作品')
        if not job.get('result_cos') or not m.settings.cos_ready():raise HTTPException(409,detail='作品尚未保存到云端')
        if body.share_original and job.get('comparison_file') and not job.get('comparison_cos'):
            raise HTTPException(409,detail='对比图尚未同步到云端，可先只投稿成品')
        if body.share_original and not (job.get('comparison_cos') or job.get('orig_cos')):raise HTTPException(400,detail='这件作品没有可公开的原图')
        reject=m._moderate_text_or_reject(title+'\n'+story,user['openid'])
        if reject:raise HTTPException(400,detail='投稿文字未通过审核，请调整后重试')
        content={'title':title,'story':story,'category':body.category,'share_original':body.share_original,
                 'author_name':user.get('nickname') or '新生创作者'}
        s=store_for(m.users)
        try:
            with s.flow_lock:
                s.maintenance();row,copy_needed=s.reserve(user['openid'],job,content)
                if not copy_needed:return {'submission':owner_view(m,row)}
                for key in keys(row):
                    m.cleanup.protect_copy_until(key,time.time()+600)
                    m.cleanup.schedule('cos',key,time.time()+3600)
            p=row['payload']
            for source,target in ((p['source_result'],p['result_key']),(p['source_orig'],p['orig_key'])):
                if not target:continue
                meta=cos.object_metadata(m.settings,source)
                if not 0<meta['size']<=32*1024*1024:raise ValueError('投稿图片为空或超过 32MB')
                cos.copy_object(m.settings,source,target,private=True)
                if cos.object_metadata(m.settings,target)['size']!=meta['size']:raise cos.CosError('投稿图片保存校验未完成',code='NETWORK')
            with s.flow_lock:
                current=s.get(row['id']);current_job=m.jobs.get(job['id'])
                if current and current['revision']==row['revision'] and current['status'] in ('pending','published'):
                    return {'submission':owner_view(m,current)}
                if (not current or current['status']!='uploading' or current['revision']!=row['revision']
                        or not current_job or current_job.get('deleted_at')):
                    enqueue_removal(m,row)
                    if current and current['status']=='uploading' and current['revision']==row['revision']:
                        s.withdraw(row['id'],user['openid'],row['revision'])
                    raise HTTPException(409,detail='作品或投稿已撤回，停止提交')
                for key in keys(row):m.cleanup.retain_until('cos',key,row['submitted']+PENDING_TTL)
                s.finish(row['id'],row['revision'])
                return {'submission':owner_view(m,s.get(row['id']))}
        except cos.CosError as exc:
            if 'row' in locals():
                with s.flow_lock:
                    s.release(row['id'],row['revision']);current=s.get(row['id'])
                    if not current or current['revision']!=row['revision'] or current['status'] not in ('uploading','pending','published'):enqueue_removal(m,row)
            raise HTTPException(503,detail='投稿图片保存暂未完成，可在我的投稿中继续提交') from exc
        except ValueError as exc:
            if 'row' in locals():s.release(row['id'],row['revision'])
            raise HTTPException(409,detail=str(exc)) from exc

    @router.get('/api/community/submissions/mine')
    def mine(request:Request,offset:int=0,limit:int=30):
        m=runtime();u=m._current_user(request);s=store_for(m.users)
        with s.flow_lock:
            s.maintenance();rows,total=s.list(owner=u['openid'],offset=offset,limit=limit)
            context = catalog_context(m)
            return {'items':[owner_view(m,r,context) for r in rows],'total':total,'next_offset':max(0,offset)+len(rows),
                    'has_more':max(0,offset)+len(rows)<total,'categories':context['categories']}

    @router.post('/api/community/submissions/{sid}/withdraw')
    def withdraw(sid:str,body:RevisionBody,request:Request):
        m=runtime();u=m._current_user(request);s=store_for(m.users)
        try:
            with s.flow_lock:
                row=s.get(sid)
                if not row or not owns(m.users,row['owner'],u['openid']):raise KeyError('投稿不存在')
                # Persist removal first. Failed physical deletion remains queued and invisible.
                s.withdraw(sid,u['openid'],body.revision);enqueue_removal(m,row)
            m.cleanup.delete_cos_now(m.settings,keys(row))
            return {'ok':True,'submission':owner_view(m,s.get(sid))}
        except KeyError:raise HTTPException(404,detail='投稿不存在')
        except ValueError as exc:raise HTTPException(409,detail=str(exc)) from exc

    @router.put('/api/community/posts/{sid}/like')
    def like(sid:str,body:LikeBody,request:Request):
        m=runtime();u=community_user(m,request);s=store_for(m.users)
        with s.flow_lock:
            base=0
            row=s.get(sid)
            if row:
                author=m.users.get_user(row['owner']) if row else None
                if not row or row['status']!='published' or not author or author.get('banned'):raise HTTPException(404,detail='帖子已下架或不存在')
            else:
                row=next((p for p in CommunityStore(m.settings).public_items() if p['id']==sid),None)
                if not row:raise HTTPException(404,detail='帖子已下架或不存在')
                base=max(0,int(row.get('likes') or 0))
            s.set_like(sid,u['openid'],body.liked);count,liked=s.likes([sid],u['openid'])[sid] if body.liked else s.likes([sid],u['openid']).get(sid,(0,False))
            return {'id':sid,'likes':base+count,'liked':liked}

    @router.get('/api/community/posts/{sid}')
    def detail(sid:str,request:Request):
        m=runtime();row=visible_post(m,sid);viewer=viewer_id(m,request)
        item=post_view(m,row);count,liked=store_for(m.users).likes([sid],viewer).get(sid,(0,False))
        item['likes']+=count;item['liked']=liked
        item['comments']=interactions_for(m.users).counts([sid]).get(sid,0)
        return {'post':item}

    @router.get('/api/community/posts/{sid}/comments')
    def comments(sid:str,request:Request,offset:int=0,limit:int=30):
        m=runtime();visible_post(m,sid)
        return interactions_for(m.users).comments(post_id=sid,viewer=viewer_id(m,request),offset=offset,limit=limit)

    @router.post('/api/community/posts/{sid}/comments')
    def add_comment(sid:str,body:CommentBody,request:Request):
        m=runtime();u=community_user(m,request);s=store_for(m.users);i=interactions_for(m.users)
        content=body.content.strip()
        if not content:raise HTTPException(400,detail='请写下留言')
        native_wechat=not u['openid'].startswith('web-') and wechat_sec.wechat_text_ready(m.settings)
        site_tencent=u.get('auth_source')=='site' and m.settings.moderation().get('enabled') and all(m.settings.tencent().get(k) for k in ('secret_id','secret_key'))
        if not native_wechat and not site_tencent:
            raise HTTPException(503,detail='微信评论审核尚未就绪，请稍后再试')
        if m.settings.maintenance().get('enabled'):raise HTTPException(503,detail='服务维护中，请稍后再试')
        try:
            with s.flow_lock:
                visible_post(m,sid);row,needed=i.reserve_comment(sid,u['openid'],body.request_id,content)
            if not needed:return {'ok':True,'id':row['id'],'duplicate':True}
        except ValueError as exc:raise HTTPException(429,detail=str(exc)) from exc
        try:
            if native_wechat:suggestion,label,_=wechat_sec.check_text(m.settings,content,u['openid'],scene=2)
            else:suggestion,label,_=moderate_text(content,m.settings)
        except (wechat_sec.WechatSecError,ModerationError) as exc:
            i.finish_comment(row['id'],'failed',exc.code)
            raise HTTPException(503,detail='文字审核暂未完成，评论尚未发布，请重试') from exc
        if suggestion.lower()!='pass':
            i.finish_comment(row['id'],'rejected',suggestion+':'+label)
            raise HTTPException(400,detail='评论未通过内容审核，请调整内容')
        with s.flow_lock:
            try:
                visible_post(m,sid);community_user(m,request)
            except HTTPException:
                i.finish_comment(row['id'],'deleted','post_unavailable');raise
            final=i.finish_comment(row['id'],'published',('wechat' if native_wechat else 'tencent')+':pass:'+label)
            if final['status']!='published':raise HTTPException(409,detail='评论已删除，停止发布')
        return {'ok':True,'id':row['id'],'duplicate':False}

    @router.delete('/api/community/comments/{cid}')
    def delete_comment(cid:str,request:Request):
        m=runtime();u=m._current_user(request)
        try:interactions_for(m.users).delete_comment(cid,u['openid']);return {'ok':True}
        except KeyError:raise HTTPException(404,detail='评论不存在')

    @router.put('/api/community/comments/{cid}/like')
    def comment_like(cid:str,body:LikeBody,request:Request):
        m=runtime();u=community_user(m,request)
        with store_for(m.users).flow_lock:
            visible_comment(m,cid)
            try:return interactions_for(m.users).like(cid,u['openid'],body.liked)
            except KeyError:raise HTTPException(404,detail='评论不存在')

    @router.post('/api/community/posts/{sid}/report')
    def post_report(sid:str,body:ReportBody,request:Request):
        m=runtime();u=community_user(m,request);row=visible_post(m,sid)
        p=row.get('payload',row)
        return save_report(m,'post',sid,sid,u['openid'],body.reason,p.get('title','')+'\n'+p.get('story',''))

    @router.post('/api/community/comments/{cid}/report')
    def comment_report(cid:str,body:ReportBody,request:Request):
        m=runtime();u=community_user(m,request);row=visible_comment(m,cid)
        return save_report(m,'comment',cid,row['post_id'],u['openid'],body.reason,row['content'])

    return router


def public_feed(m,request,offset=0,limit=200,liked_only=False,category='all'):
    context = catalog_context(m)
    if category != 'all' and category not in context['names'] and category not in CATEGORIES:
        raise HTTPException(400,detail='社区分类无效')
    legacy = category != 'all' and category not in context['names'] and category in CATEGORIES
    viewer=None
    if request.headers.get('authorization') or getattr(request,'cookies',{}).get('site_session'):
        try:viewer=m._current_user(request)['openid']
        except HTTPException:pass  # Viewing is public; writes still require a valid session.
    if liked_only:
        viewer=m._current_user(request)['openid']
    s=store_for(m.users);items=[];liked_ids=None;editorial_candidates=[]
    offset=max(0,offset);limit=max(1,min(200,limit))
    if liked_only:
        with m.users._lock:
            viewers=aliases_unlocked(m.users,viewer)
            liked_ids={r[0] for r in m.users._conn.execute('SELECT post_id FROM community_likes WHERE owner IN ('+marks(viewers)+')',viewers)}
    for p in CommunityStore(m.settings).public_items():
        if liked_ids is not None and p['id'] not in liked_ids:continue
        fields = category_fields(m,p,context,legacy)
        if category!='all' and fields['category']!=category:continue
        editorial_candidates.append(p)
        items.append({'id':p['id'],'title':p.get('title',''),'story':p.get('story',''),
            'authorName':p.get('author_name',''),'authorAvatar':p.get('author_avatar',''),'date':p.get('date',''),
            **fields,
            'templateId':p.get('template_id',''),'templateName':p.get('template_name',''),
            'quality':p.get('quality','light'),'resultUrl':p.get('result_url',''),'origUrl':p.get('orig_url',''),
            'thumbnailUrl':p.get('result_url',''),
            'likes':m._community_likes(p.get('likes')),'liked':False,'pinned':bool(p.get('pinned')),
            'featured':False,'_sort':(not p.get('pinned'),p.get('sort',100),-float(p.get('created_at') or 0),p['id'])})
    candidates,total=s.public_page(editorial_candidates,offset=offset,limit=limit,category=category,
        liked_viewer=viewer if liked_only else None,cos_ready=m.settings.cos_ready(),
        template_groups=context['template_groups'],catalog_groups=context['names'],legacy_category=legacy)
    editorial_views={p['id']:p for p in items}
    submission_rows=s.public_rows([p['id'] for p in candidates if p['source']=='submission'])
    items=[]
    for candidate in candidates:
        if candidate['source']=='editorial':
            items.append(editorial_views[candidate['id']]);continue
        row=submission_rows.get(candidate['id'])
        if not row:continue
        p=row['payload']
        fields = category_fields(m,p,context,legacy)
        if category!='all' and fields['category']!=category:continue
        if not p.get('result_key'):continue
        items.append({'id':row['id'],'title':p['title'],'story':p['story'],'authorName':p['author_name'],'authorAvatar':'',
            'date':datetime.fromtimestamp(row['submitted'],timezone(timedelta(hours=8))).strftime('%Y.%m.%d'),
            **fields,'templateId':p['template_id'],
            'templateName':p['template_name'],'quality':p['quality'],'resultUrl':'','origUrl':'',
            '_media_keys':(p['result_key'],p['orig_key'] if p['share_original'] else ''),
            'likes':0,'liked':False,'pinned':bool(row['featured']),'featured':bool(row['featured']),
            '_sort':(not row['featured'],100,-row['submitted'],row['id'])})
    likes=s.likes([p['id'] for p in items],viewer)
    counts=interactions_for(m.users).counts([p['id'] for p in items])
    for p in items:
        media_keys=p.pop('_media_keys',None)
        if media_keys:
            p['resultUrl']=signed(m,media_keys[0]);p['origUrl']=signed(m,media_keys[1])
            p['thumbnailUrl']=signed(m,media_keys[0],thumbnail=True)
        else:p['thumbnailUrl']=editorial_thumbnail(m.settings,p.get('resultUrl',''))
        count,liked=likes.get(p['id'],(0,False));p['likes']+=count;p['liked']=liked;p.pop('_sort',None)
        p['comments']=counts.get(p['id'],0)
    return {'enabled':True,'items':items,'featured_reward':m.settings.rewards()['community_featured'],
            'has_more':offset+len(candidates)<total,'next_offset':offset+len(candidates),'total':total,'submissions_enabled':True,
            'categories':context['categories'],'category_semantics':'legacy' if legacy else 'template_group','legacySemantics':legacy}


def post_view(m,row):
    if 'payload' in row:
        p=row['payload']
        return {'id':row['id'],'title':p['title'],'story':p['story'],'authorName':p['author_name'],'authorAvatar':'',
                'date':datetime.fromtimestamp(row['submitted'],timezone(timedelta(hours=8))).strftime('%Y.%m.%d'),
                **category_fields(m,p),
                'templateId':p['template_id'],'templateName':p['template_name'],'quality':p['quality'],
                'resultUrl':signed(m,p['result_key']),'origUrl':signed(m,p['orig_key']) if p['share_original'] else '',
                'thumbnailUrl':signed(m,p['result_key'],thumbnail=True),
                'likes':0,'liked':False,'featured':bool(row['featured']),'pinned':bool(row['featured'])}
    fields={'authorName':'author_name','authorAvatar':'author_avatar','categoryName':'category_name',
            'templateId':'template_id','templateName':'template_name','resultUrl':'result_url','origUrl':'orig_url'}
    return {**{k:row.get(k,'') for k in ('id','title','story','date','category','quality')},
            **{k:row.get(v,'') for k,v in fields.items()},**category_fields(m,row),'likes':m._community_likes(row.get('likes')),
            'thumbnailUrl':editorial_thumbnail(m.settings,row.get('result_url','')),
            'liked':False,'featured':False,'pinned':bool(row.get('pinned'))}


def save_report(m,kind,tid,pid,owner,reason,snapshot):
    if reason not in REPORT_REASONS:raise HTTPException(400,detail='请选择举报原因')
    try:return interactions_for(m.users).report(kind,tid,pid,owner,reason,snapshot)
    except ValueError as exc:raise HTTPException(429,detail=str(exc)) from exc


def install_admin_routes(router,guard,runtime):
    @router.get('/community/comments')
    def admin_comments(request:Request,offset:int=0,limit:int=30):
        guard(request);return interactions_for(runtime().users).comments(offset=offset,limit=limit,admin=True)

    @router.delete('/community/comments/{cid}')
    def remove_comment(cid:str,request:Request):
        guard(request)
        try:interactions_for(runtime().users).delete_comment(cid);return {'ok':True}
        except KeyError:raise HTTPException(404,detail='评论不存在')

    @router.get('/community/reports')
    def reports(request:Request,status:str='open',offset:int=0,limit:int=30):
        guard(request)
        try:return interactions_for(runtime().users).reports(status,offset,limit)
        except ValueError as exc:raise HTTPException(400,detail=str(exc)) from exc

    @router.post('/community/reports/{rid}/resolve')
    def resolve_report(rid:str,request:Request):
        guard(request)
        try:interactions_for(runtime().users).resolve_report(rid);return {'ok':True}
        except KeyError:raise HTTPException(404,detail='举报不存在')

    @router.get('/community/submissions')
    def listing(request:Request,status:str='pending',offset:int=0,limit:int=30):
        guard(request);m=runtime();s=store_for(m.users)
        try:
            with s.flow_lock:
                s.maintenance();rows,total=s.list(status=status,offset=offset,limit=limit)
                context=catalog_context(m)
                return {'items':[owner_view(m,r,context) for r in rows],'total':total,'reward':m.settings.rewards()['community_featured']}
        except ValueError as exc:raise HTTPException(400,detail=str(exc)) from exc

    @router.post('/community/submissions/{sid}/review')
    async def review(sid:str,request:Request):
        guard(request);m=runtime();s=store_for(m.users)
        # Admin-authenticated structured payload; content limits are validated below.
        from admin_api import _json_body
        body=await _json_body(request)
        action=body.get('action');revision=body.get('revision');reason=body.get('reason','')
        if type(revision) is not int or not isinstance(reason,str) or len(reason)>500:raise HTTPException(400,detail='审核参数无效')
        # COS I/O is synchronous and belongs off the async request event loop.
        from starlette.concurrency import run_in_threadpool
        def apply():
            try:
                row=s.get(sid)
                if not row:raise KeyError('投稿不存在')
                if action=='approve':
                    for key in keys(row):
                        if cos.object_metadata(m.settings,key)['size']<=0:raise ValueError('投稿图片已过期，请通知作者重新投稿')
                with s.flow_lock:
                    updated=s.review(sid,revision,action,reason,m.settings.rewards()['community_featured'])
                    if action=='approve':
                        for key in keys(updated):m.cleanup.cancel('cos',key)
                    elif action in ('reject','unpublish'):enqueue_removal(m,updated)
                    return {'ok':True,'submission':owner_view(m,updated)}
            except KeyError:raise HTTPException(404,detail='投稿不存在')
            except ValueError as exc:raise HTTPException(409,detail=str(exc)) from exc
            except cos.CosError as exc:raise HTTPException(503,detail='投稿图片尚未就绪，请稍后重试') from exc
        return await run_in_threadpool(apply)
