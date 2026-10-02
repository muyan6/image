"""Strict owner/admin template-sharing routes and recoverable catalog publication."""
import logging
import time
from typing import Annotated
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr
import cos_store as cos
from template_shares import store_for, MAX_COVER_BYTES, UPLOAD_TTL, PENDING_TTL, ShareLimitError

log=logging.getLogger('rescue.template_sharing')


class StrictBody(BaseModel):
    model_config=ConfigDict(extra='forbid')


class GuideBody(StrictBody):
    advice: StrictStr=Field(default='',max_length=500)
    suitable: list[Annotated[StrictStr,Field(max_length=60)]]=Field(default_factory=list,max_length=8)
    unsuitable: list[Annotated[StrictStr,Field(max_length=60)]]=Field(default_factory=list,max_length=8)
    tips: list[Annotated[StrictStr,Field(max_length=60)]]=Field(default_factory=list,max_length=8)


class ContentBody(StrictBody):
    name: StrictStr=Field(default='',max_length=20)
    subtitle: StrictStr=Field(default='',max_length=60)
    prompt: StrictStr=Field(default='',max_length=4000)
    guide: GuideBody=Field(default_factory=GuideBody)
    cover_tokens: list[StrictStr]=Field(default_factory=list,max_length=3)
    submit: StrictBool=False
    consent: StrictBool=False


class CreateBody(ContentBody):
    request_id: StrictStr=Field(pattern=r'^[A-Za-z0-9_-]{8,64}$')


class UpdateBody(ContentBody):
    revision: StrictInt=Field(ge=1)


class UploadBody(StrictBody):
    share_id: StrictStr=Field(pattern=r'^ts_[0-9a-f]{32}$')
    revision: StrictInt=Field(ge=1)
    ext: StrictStr=Field(pattern=r'^(jpg|png|webp)$')
    size: StrictInt=Field(gt=0,le=MAX_COVER_BYTES)
    content_type: StrictStr=Field(pattern=r'^image/(jpeg|png|webp)$')


class CompleteBody(StrictBody):
    share_id: StrictStr=Field(pattern=r'^ts_[0-9a-f]{32}$')
    revision: StrictInt=Field(ge=1)


class RevisionBody(StrictBody):
    revision: StrictInt=Field(ge=1)


class ReviewPatch(StrictBody):
    name: StrictStr|None=Field(default=None,max_length=20)
    subtitle: StrictStr|None=Field(default=None,max_length=60)
    prompt: StrictStr|None=Field(default=None,max_length=4000)
    guide: GuideBody|None=None
    group_id: StrictStr|None=Field(default=None,pattern=r'^[a-z0-9_]{0,24}$')


class ReviewBody(RevisionBody):
    action: StrictStr=Field(pattern=r'^(approve|reject|unpublish)$')
    reason: StrictStr=Field(default='',max_length=500)
    patch: ReviewPatch=Field(default_factory=ReviewPatch)


def config(settings):
    conf=settings.template_sharing() if hasattr(settings,'template_sharing') else {}
    return {'enabled':bool(conf.get('enabled',True)),'reward_enabled':bool(conf.get('reward_enabled',True))}


def bind(m):
    store=store_for(m.users)
    m.templates.bind_shared_visibility(lambda ids:store.visible_template_ids(ids,config(m.settings)['enabled']),
                                      owner=store,settings=m.settings)
    return store


def _require_admin(request):
    # The existing admin guard is factory-local, so reuse its exact primitives.
    from admin_api import COOKIE_NAME, _verify_token, _password
    if request.headers.get('x-admin-request')!='1':raise HTTPException(403,detail='缺少请求头')
    token=request.cookies.get(COOKIE_NAME,'')
    if not token or not _verify_token(token,_password()):raise HTTPException(401,detail='未登录或会话已过期')


def _user(m,request,write=False):
    user=m._current_user(request)
    if write:
        if user.get('banned'):raise HTTPException(403,detail='账号已被封禁，暂不能投稿')
        if not config(m.settings)['enabled']:raise HTTPException(503,detail='模板分享暂未开放')
        if m.settings.maintenance().get('enabled'):raise HTTPException(503,detail='服务维护中，请稍后投稿')
    return user


def _cover_view(m,upload):
    try:
        preview=cos.presign(m.settings,'get',upload['final_key'],ttl_seconds=300)
        thumbnail=cos.thumbnail_url(m.settings,upload['final_key'],ttl_seconds=300)
    except cos.CosError:preview=thumbnail=''
    return {'token':upload['token'],'preview_url':preview,
            'thumbnail_url':thumbnail,
            'width':upload['width'],'height':upload['height']}


def owner_view(m,row):
    payload=row['payload'];uploads=store_for(m.users).covers(row)
    return {'id':row['id'],'revision':row['revision'],'status':row['status'],**payload,
            'covers':[_cover_view(m,u) for u in uploads], 'author_name':row['author_name'],
            'has_published':bool(row['catalog_enabled'] and row['applied_enabled'] and row['applied_revision']>0),
            'published_revision':row['applied_revision'],'published_template_id':row['tpl_id'],
            'template_id':row['tpl_id'],
            'consent_version':row['consent_version'],
            'reason':row['reason'],'created_at':row['created'],'updated_at':row['updated'],
            'published_at':row['published_at']}


def _error(exc):
    if isinstance(exc,KeyError):return HTTPException(404,detail='投稿或上传登记不存在')
    if isinstance(exc,ShareLimitError):return HTTPException(429,detail=str(exc))
    if isinstance(exc,cos.CosError):return HTTPException(503,detail='云端封面确认暂未完成，请稍后重试')
    if isinstance(exc,OSError):return HTTPException(503,detail='审核已保存，模板发布同步待恢复')
    return HTTPException(409,detail=str(exc))


def _check_image(settings,upload):
    """HEAD + at most 16 magic bytes + CI dimensions; no full image download/relay."""
    key=upload['final_key'];meta=cos.object_metadata(settings,key)
    if not 0<meta['size']<=MAX_COVER_BYTES or meta['size']!=upload['declared_size']:
        raise ValueError('云端封面大小与登记不一致')
    if meta.get('content_type','').split(';',1)[0].lower()!=upload['content_type']:
        raise ValueError('云端封面类型与登记不一致')
    with cos.control_request(settings,'GET',key,headers={'Range':'bytes=0-15'},stream=True) as response:
        if response.status_code not in (200,206):raise cos.response_error(response,'封面文件校验失败',key,streamed=True)
        magic=b''
        for chunk in response.iter_content(16):
            magic+=chunk[:16-len(magic)]
            if len(magic)>=16:break
    kind=('jpg' if magic.startswith(b'\xff\xd8\xff') else 'png' if magic.startswith(b'\x89PNG\r\n\x1a\n')
          else 'webp' if len(magic)>=12 and magic[:4]==b'RIFF' and magic[8:12]==b'WEBP' else '')
    if kind!=upload['ext']:raise ValueError('封面文件内容与扩展名不一致')
    info=cos.image_info(settings,key);width=info['width'];height=info['height']
    if not 64<=width<=12000 or not 64<=height<=12000 or width*height>40_000_000 or not .25<=width/height<=4:
        raise ValueError('封面请使用清晰且比例合理的图片（宽高比 1:4 至 4:1）')
    return width,height


def apply_projection(m,row):
    store=bind(m)
    if row['catalog_enabled']:
        m.templates.upsert_shared(tpl_id=row['tpl_id'],share_id=row['id'],source_revision=row['approved_revision'],
            author_openid=row['owner'],author_name=row['author_name'],payload=row['approved_payload'],published_at=row['published_at'])
        if getattr(m,'cleanup',None):
            for ref in row['approved_payload']['covers']:m.cleanup.cancel('cos',ref[4:])
    else:m.templates.disable_shared(row['tpl_id'],row['id'])
    store.mark_applied(row)


def reconcile(m):
    """Call once after users/templates initialization, before opening app routes."""
    store=bind(m);result={'checked':0,'applied':0,'failed':0};after=''
    with store.flow_lock:
        while True:
            rows=store.recovery_rows(after_id=after,limit=200)
            if not rows:break
            for row in rows:
                result['checked']+=1
                try:
                    with store.row_lock(row['id']):
                        current=store.get(row['id'])
                        if current:apply_projection(m,current);result['applied']+=1
                except (ValueError,OSError) as exc:
                    result['failed']+=1;log.error('模板分享投影待恢复 %s: %s',row['id'],type(exc).__name__)
            after=rows[-1]['id']
    return result


def make_template_share_router(runtime):
    router=APIRouter()

    @router.post('/api/template-shares')
    def create(body:CreateBody,request:Request):
        m=runtime();user=_user(m,request,True);store=bind(m)
        if body.submit:raise HTTPException(400,detail='请先创建草稿并上传封面')
        try:
            with store.flow_lock:
                row=store.create(user['openid'],body.request_id,body.model_dump(exclude={'request_id','submit','consent'}),user.get('nickname') or '分享作者')
            return {'submission':owner_view(m,row)}
        except (KeyError,ValueError,cos.CosError) as exc:raise _error(exc) from exc

    @router.get('/api/template-shares/mine')
    def mine(request:Request,status:str='all',offset:int=0,limit:int=24):
        m=runtime();user=_user(m,request);store=bind(m)
        try:rows,total=store.list(owner=user['openid'],status=status,offset=offset,limit=limit)
        except ValueError as exc:raise _error(exc) from exc
        from template_share_rewards import reward_summary
        return {'items':[owner_view(m,row) for row in rows],'total':total,'next_offset':max(0,offset)+len(rows),'has_more':max(0,offset)+len(rows)<total,'enabled':config(m.settings)['enabled'],'rewards':reward_summary(m.users,user['openid'])}

    @router.post('/api/template-shares/uploads')
    def upload(body:UploadBody,request:Request):
        m=runtime();user=_user(m,request,True);store=bind(m)
        if not m.settings.cos_ready():raise HTTPException(503,detail='对象存储未配置')
        try:
            with store.row_lock(body.share_id):
                rec=store.issue_upload(body.share_id,user['openid'],body.revision,body.ext,body.size,body.content_type)
                headers={'Content-Type':rec['content_type'],'x-cos-acl':'private'}
                url=cos.presign(m.settings,'put',rec['temp_key'],ttl_seconds=UPLOAD_TTL,headers=headers)
                if getattr(m,'cleanup',None):
                    m.cleanup.protect_upload_until(rec['temp_key'],rec['expires'])
                    m.cleanup.schedule('cos',rec['temp_key'],rec['expires'])
            return {'upload_token':rec['token'],'url':url,'headers':headers,'expires_at':rec['expires']}
        except (KeyError,ValueError,cos.CosError) as exc:raise _error(exc) from exc

    @router.post('/api/template-shares/uploads/{token}/complete')
    def complete(token:str,body:CompleteBody,request:Request):
        m=runtime();user=_user(m,request,True);store=bind(m)
        try:
            with store.row_lock(body.share_id):
                upload,row=store.upload(token,body.share_id,user['openid'],body.revision)
                if upload['status']=='ready':
                    width,height=upload['width'],upload['height']
                else:
                    meta=cos.object_metadata(m.settings,upload['temp_key'])
                    if meta['size']!=upload['declared_size'] or not 0<meta['size']<=MAX_COVER_BYTES:raise ValueError('上传封面大小与登记不一致')
                    if getattr(m,'cleanup',None):
                        m.cleanup.protect_copy_until(upload['final_key'],time.time()+600)
                        m.cleanup.schedule('cos',upload['final_key'],time.time()+PENDING_TTL)
                    cos.copy_object(m.settings,upload['temp_key'],upload['final_key'],private=True)
                    width,height=_check_image(m.settings,upload)
                row=store.finish_upload(token,body.share_id,user['openid'],body.revision,width,height)
                cover=next(u for u in store.covers(row) if u['token']==token)
            return {'cover':_cover_view(m,cover),'submission':owner_view(m,row)}
        except (KeyError,ValueError,cos.CosError) as exc:raise _error(exc) from exc

    @router.get('/api/template-shares/{sid}')
    def detail(sid:str,request:Request):
        m=runtime();user=_user(m,request);store=bind(m)
        try:return {'submission':owner_view(m,store.get(sid,user['openid']))}
        except (KeyError,ValueError,cos.CosError) as exc:raise _error(exc) from exc

    @router.put('/api/template-shares/{sid}')
    def update(sid:str,body:UpdateBody,request:Request):
        m=runtime();user=_user(m,request,True);store=bind(m)
        try:
            with store.row_lock(sid):
                old=store.get(sid,user['openid'])
                if body.submit and not body.consent:raise HTTPException(400,detail='请确认模板内容与封面的公开分享授权')
                raw=body.model_dump(exclude={'revision','submit','consent'},exclude_unset=True)
                if body.submit:
                    tokens=raw.get('cover_tokens',old['payload']['cover_tokens'])
                    for cover in store.covers(old,tokens):
                        meta=cos.object_metadata(m.settings,cover['final_key'])
                        if meta['size']!=cover['declared_size']:raise ValueError('封面已失效，请重新上传')
                row=store.update(sid,user['openid'],body.revision,raw,body.submit,body.consent)
            return {'submission':owner_view(m,row)}
        except (KeyError,ValueError,cos.CosError) as exc:raise _error(exc) from exc

    @router.post('/api/template-shares/{sid}/withdraw')
    def withdraw(sid:str,body:RevisionBody,request:Request):
        m=runtime();user=_user(m,request,True);store=bind(m)
        try:
            with store.row_lock(sid):
                row=store.withdraw(sid,body.revision,user['openid']);apply_projection(m,row)
            return {'ok':True,'submission':owner_view(m,store.get(sid,user['openid']))}
        except (KeyError,ValueError,OSError,cos.CosError) as exc:raise _error(exc) from exc

    @router.get('/admin/api/template-shares')
    def admin_list(request:Request,status:str='pending',offset:int=0,limit:int=24):
        _require_admin(request);m=runtime();store=bind(m)
        try:rows,total=store.list(status=status,offset=offset,limit=limit)
        except ValueError as exc:raise _error(exc) from exc
        from template_share_rewards import reward_summary
        summaries={};items=[]
        for row in rows:
            if row['owner'] not in summaries:summaries[row['owner']]=reward_summary(m.users,row['owner'])
            items.append(dict(owner_view(m,row),reward_summary=summaries[row['owner']]))
        return {'items':items,'total':total,'next_offset':max(0,offset)+len(rows),'has_more':max(0,offset)+len(rows)<total}

    @router.get('/admin/api/template-shares/{sid}')
    def admin_detail(sid:str,request:Request):
        _require_admin(request);m=runtime();store=bind(m);row=store.get(sid)
        if not row:raise HTTPException(404,detail='投稿不存在')
        from template_share_rewards import reward_summary
        return {'submission':dict(owner_view(m,row),reward_summary=reward_summary(m.users,row['owner']))}

    @router.post('/admin/api/template-shares/{sid}/review')
    def review(sid:str,body:ReviewBody,request:Request):
        _require_admin(request);m=runtime();store=bind(m)
        patch=body.patch.model_dump(exclude_none=True,exclude_unset=True)
        if patch.get('group_id') and patch['group_id'] not in {g['id'] for g in m.templates.list_groups(enabled_only=True)}:
            raise HTTPException(400,detail='请选择现有启用分类')
        try:
            with store.row_lock(sid):
                if body.action=='approve':
                    if not config(m.settings)['enabled']:raise HTTPException(503,detail='模板分享暂未开放')
                    old=store.get(sid)
                    if not old:raise KeyError('投稿不存在')
                    for cover in store.covers(old):
                        if cos.object_metadata(m.settings,cover['final_key'])['size']!=cover['declared_size']:raise ValueError('封面已失效，请通知作者重新上传')
                    row=store.prepare_approve(sid,body.revision,patch);apply_projection(m,row)
                elif body.action=='reject':store.reject(sid,body.revision,body.reason)
                else:
                    if not body.reason.strip():raise ValueError('请填写下架原因')
                    row=store.withdraw(sid,body.revision,reason=body.reason);apply_projection(m,row)
            return {'ok':True,'submission':owner_view(m,store.get(sid))}
        except (KeyError,ValueError,OSError,cos.CosError) as exc:raise _error(exc) from exc

    return router
