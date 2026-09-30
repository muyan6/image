"""Consent-bound community publication; no client-selected media keys or rewards."""
import time
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, StrictBool, Field
import cos_store as cos
from community_store import CommunityStore, CATEGORIES
from community_submissions import store_for, PENDING_TTL


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


def keys(row):
    return [row['payload'][k] for k in ('result_key','orig_key') if row['payload'].get(k)]


def signed(m, key):
    if not key or not m.settings.cos_ready():return ''
    return cos.presign(m.settings,'get',key,ttl_seconds=300)


def owner_view(m, row):
    p=row['payload'];visible=row['status'] in ('pending','published')
    return {'id':row['id'],'job_id':row['job_id'],'revision':row['revision'],'status':row['status'],
            'title':p['title'],'story':p['story'],'category':p['category'],'author_name':p['author_name'],
            'share_original':p['share_original'],'reason':row['reason'],'featured':bool(row['featured']),
            'reward':row['reward'],'rewarded':store_for(m.users).rewarded(row['id']),'submitted_at':row['submitted'],
            'result_url':signed(m,p['result_key']) if visible else '',
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

    @router.post('/api/community/submissions')
    def submit(body:SubmissionBody,request:Request):
        m=runtime();user=community_user(m,request)
        if not body.consent:raise HTTPException(400,detail='请先确认社区公开展示授权')
        if m.settings.maintenance().get('enabled'):raise HTTPException(503,detail='服务维护中，请稍后投稿')
        title=body.title.strip();story=body.story.strip()
        if not title or body.category not in CATEGORIES:raise HTTPException(400,detail='请填写标题并选择有效分类')
        job=m.jobs.get(m._safe_job_id(body.job_id))
        if not job or job.get('openid')!=user['openid'] or job.get('deleted_at'):
            raise HTTPException(404,detail='作品不存在')
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
            return {'items':[owner_view(m,r) for r in rows],'total':total,'next_offset':max(0,offset)+len(rows),
                    'has_more':max(0,offset)+len(rows)<total}

    @router.post('/api/community/submissions/{sid}/withdraw')
    def withdraw(sid:str,body:RevisionBody,request:Request):
        m=runtime();u=m._current_user(request);s=store_for(m.users)
        try:
            with s.flow_lock:
                row=s.get(sid)
                if not row or row['owner']!=u['openid']:raise KeyError('投稿不存在')
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
                row=next((p for p in CommunityStore(m.settings).list(status='published',limit=200)['items'] if p['id']==sid),None)
                if not row:raise HTTPException(404,detail='帖子已下架或不存在')
                base=max(0,int(row.get('likes') or 0))
            s.set_like(sid,u['openid'],body.liked);count,liked=s.likes([sid],u['openid'])[sid] if body.liked else s.likes([sid],u['openid']).get(sid,(0,False))
            return {'id':sid,'likes':base+count,'liked':liked}

    return router


def public_feed(m,request,offset=0,limit=200):
    viewer=None
    if request.headers.get('authorization'):
        try:viewer=m._current_user(request)['openid']
        except HTTPException:pass  # Viewing is public; writes still require a valid session.
    s=store_for(m.users);items=[]
    for p in CommunityStore(m.settings).list(status='published',limit=200)['items']:
        items.append({'id':p['id'],'title':p.get('title',''),'story':p.get('story',''),
            'authorName':p.get('author_name',''),'authorAvatar':p.get('author_avatar',''),'date':p.get('date',''),
            'category':p.get('category','all'),'categoryName':p.get('category_name',''),
            'templateId':p.get('template_id',''),'templateName':p.get('template_name',''),
            'quality':p.get('quality','light'),'resultUrl':p.get('result_url',''),'origUrl':p.get('orig_url',''),
            'likes':m._community_likes(p.get('likes')),'liked':False,'pinned':bool(p.get('pinned')),
            'featured':False,'_sort':(not p.get('pinned'),p.get('sort',100),-float(p.get('created_at') or 0),p['id'])})
    for row in s.list(status='published',limit=200)[0]:
        author=m.users.get_user(row['owner'])
        if not author or author.get('banned'):continue
        p=row['payload'];result=signed(m,p['result_key'])
        if not result:continue
        items.append({'id':row['id'],'title':p['title'],'story':p['story'],'authorName':p['author_name'],'authorAvatar':'',
            'date':datetime.fromtimestamp(row['submitted'],timezone(timedelta(hours=8))).strftime('%Y.%m.%d'),
            'category':p['category'],'categoryName':CATEGORIES[p['category']],'templateId':p['template_id'],
            'templateName':p['template_name'],'quality':p['quality'],'resultUrl':result,
            'origUrl':signed(m,p['orig_key']) if p['share_original'] else '',
            'likes':0,'liked':False,'pinned':bool(row['featured']),'featured':bool(row['featured']),
            '_sort':(not row['featured'],100,-row['submitted'],row['id'])})
    items.sort(key=lambda p:p['_sort']);total=len(items);offset=max(0,offset);limit=max(1,min(200,limit));items=items[offset:offset+limit]
    likes=s.likes([p['id'] for p in items],viewer)
    for p in items:
        count,liked=likes.get(p['id'],(0,False));p['likes']+=count;p['liked']=liked;p.pop('_sort',None)
    return {'enabled':True,'items':items,'featured_reward':m.settings.rewards()['community_featured'],
            'has_more':offset+len(items)<total,'next_offset':offset+len(items),'total':total,'submissions_enabled':True}


def install_admin_routes(router,guard,runtime):
    @router.get('/community/submissions')
    def listing(request:Request,status:str='pending',offset:int=0,limit:int=30):
        guard(request);m=runtime();s=store_for(m.users)
        try:
            with s.flow_lock:
                s.maintenance();rows,total=s.list(status=status,offset=offset,limit=limit)
                return {'items':[owner_view(m,r) for r in rows],'total':total,'reward':m.settings.rewards()['community_featured']}
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
