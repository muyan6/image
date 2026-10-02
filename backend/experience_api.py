"""User-facing experience endpoints; no provider calls or balance mutations."""
import time
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictBool
from experience_store import store_for


class PreferenceBody(BaseModel):
    template_id: str = Field(min_length=1, max_length=100)
    favorite: StrictBool


class RecentBody(BaseModel):
    template_id: str = Field(min_length=1, max_length=100)


def available_templates(m):
    groups = {g['id'] for g in m.templates.list_groups(enabled_only=True)}
    return {t['id'] for t in m.templates.list_templates(enabled_only=True) if t.get('group_id') in groups}


def submission_view(m, openid, row):
    state = row['state']; job = m.jobs.get(row['job_id'])
    result = {'code': 0, 'client_request_id': row['request_id'], 'state': state,
              'submission_state': state, 'job_id': None, 'input_mode': row['input_mode']}
    if job and job.get('openid') == openid:
        state = 'accepted'
        store_for(m.users).settle(openid, row['request_id'], state)
        result.update(state=state, submission_state=state, job_id=job['id'], status=job['status'],
                      quality=job.get('quality'), balance=m.users.get_balance(openid),
                      price=job.get('price'), free_mode=job.get('charged_amount') == 0,
                      orig_url=m._job_media_url(job, 'orig'), result_url=m._job_media_url(job, 'result'))
    elif state == 'accepted':
        # Retained request receipt still proves acceptance when the job expires.
        result.update(job_id=row['job_id'], status='expired', detail='任务已删除或到保存期限')
    elif state == 'rejected':
        result.update(detail=row['error'], http_status=row['http_status'])
    else:
        result.update(status=state, detail='提交结果确认中，请保留提交编号，勿重复生成')
    return result


def idempotent_submit(m, user, request_id, payload, input_mode, submit):
    if not request_id:
        return submit(None)
    store = store_for(m.users)
    try:
        row, claimed = store.claim(user['openid'], request_id, payload, input_mode)
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    if not claimed:
        view = submission_view(m, user['openid'], row)
        if view['state'] in ('pending', 'uncertain'):
            return JSONResponse(status_code=202, content=view)
        if view['state'] == 'rejected':
            return JSONResponse(status_code=view.get('http_status') or 400, content=view)
        return view
    try:
        response = submit(row['job_id'])
    except Exception as exc:
        job = m.jobs.get(row['job_id'])
        settlement = store.settlement(user['openid'], row['job_id'])
        if job and job.get('openid') == user['openid']:
            store.settle(user['openid'], request_id, 'accepted')
        elif isinstance(exc, HTTPException) and exc.status_code < 500 and settlement['charged_amount'] is None:
            store.settle(user['openid'], request_id, 'rejected', exc.detail, exc.status_code)
        else:
            # A 5xx, interrupted admission or debit without a job is ambiguous.
            # Never clear its request key and pretend it was not received.
            store.settle(user['openid'], request_id, 'uncertain')
        raise
    store.settle(user['openid'], request_id, 'accepted')
    return {**response, 'client_request_id': request_id, 'state': 'accepted', 'submission_state': 'accepted'}


def photo_recipe(quality, template, text_values, aspect_ratio, custom_prompt, output_mode):
    return {'input_mode': 'photo', 'quality': quality, 'template_id': (template or {}).get('id', ''),
            'text_fields': dict(text_values or {}), 'custom_prompt': custom_prompt or '',
            'aspect_ratio': '' if template else aspect_ratio or '',
            'template_output_mode': (template or {}).get('output_mode', output_mode or ''),
            'prompt': (template or {}).get('prompt', '')}


def make_experience_router(runtime):
    router = APIRouter()

    @router.get('/api/me/credits')
    def credits(request: Request, offset: int = 0, limit: int = 30):
        m = runtime(); user = m._current_user(request)
        return store_for(m.users).credit_records(user['openid'], offset, limit)

    @router.get('/api/me/preferences')
    def preferences(request: Request):
        m = runtime(); user = m._current_user(request)
        return store_for(m.users).preferences(user['openid'], available_templates(m))

    @router.put('/api/me/preferences')
    def set_preference(body: PreferenceBody, request: Request):
        m = runtime(); user = m._current_user(request); available = available_templates(m)
        if body.template_id not in available:
            raise HTTPException(400, detail='模板不存在或已下架')
        store = store_for(m.users); store.preference(user['openid'], body.template_id, body.favorite)
        return store.preferences(user['openid'], available)

    @router.post('/api/me/recent-template')
    def recent_template(body: RecentBody, request: Request):
        m = runtime(); user = m._current_user(request); available = available_templates(m)
        if body.template_id not in available:
            raise HTTPException(400, detail='模板不存在或已下架')
        store = store_for(m.users); store.preference(user['openid'], body.template_id, recent=True)
        return store.preferences(user['openid'], available)

    @router.get('/api/me/submissions/{request_id}')
    def find_submission(request_id: str, request: Request):
        m = runtime(); user = m._current_user(request)
        row = store_for(m.users).submission(user['openid'], request_id)
        if not row:
            return JSONResponse(status_code=404, content={'detail': '提交登记不存在', 'state': 'not_found',
                                                         'submission_state': 'not_found', 'job_id': None})
        view = submission_view(m, user['openid'], row)
        return JSONResponse(status_code=202 if view['state'] in ('pending', 'uncertain') else 200, content=view)

    @router.get('/api/jobs/{job_id}/recipe')
    def recipe(job_id: str, request: Request):
        m = runtime(); user = m._current_user(request); job = m.jobs.get(m._safe_job_id(job_id))
        if not job or job.get('openid') != user['openid'] or job.get('deleted_at'):
            raise HTTPException(404, detail='作品不存在')
        if job.get('status') != 'succeeded':
            raise HTTPException(409, detail='作品尚未完成')
        if time.time() >= m._media_expires_at(job, 'result'):
            raise HTTPException(410, detail='作品已到保存期限')
        saved = job.get('recipe')
        names = ('input_mode', 'quality', 'template_id', 'text_fields', 'custom_prompt', 'aspect_ratio',
                 'template_output_mode', 'prompt')
        # Old jobs retain only some public metadata. Null signals unavailable
        # values; current settings/defaults must not replace historical intent.
        result = {key: saved.get(key) if isinstance(saved, dict) else job.get(key)
                  for key in names}
        mode = result.get('input_mode') or job.get('input_mode', 'photo'); result['input_mode'] = mode
        required = ('prompt', 'aspect_ratio') if mode == 'text' else names[:-1]
        missing = [key for key in required if not isinstance(saved, dict) or key not in saved]
        original = {k: v for k, v in job.items() if k not in ('comparison_file', 'comparison_cos')}
        result['orig_url'] = m._job_media_url(original, 'orig') if mode == 'photo' else None
        if mode == 'photo' and not result['orig_url']: missing.append('orig_url')
        result.update(job_id=job['id'], complete=not missing, recipe_complete=not missing, missing_fields=missing)
        return result

    return router
