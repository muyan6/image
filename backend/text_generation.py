"""Text-to-image jobs share account admission, queue and COS delivery."""
import os,time,uuid
from fastapi import APIRouter,HTTPException,Request
from pydantic import BaseModel
from gateway_ai import OpenAIImagesEnhance
from image_processing import QueueFull
from gateway_profiles import text_gateway

SIZES={'1:1':'1024x1024','3:2':'1536x1024','2:3':'1024x1536'}

class TextBody(BaseModel):
    prompt:str=''
    aspect_ratio:str='1:1'

def ready(m, conf=None):
    conf=m.settings.text_generation() if conf is None else conf;provider=text_gateway(m.settings,conf)
    return bool(conf['enabled'] and conf['model'] and provider.get('base_url') and provider.get('api_key') and m.settings.cos_ready()
                and (not m.cloud.enabled() or m.cloud.ready(text=True)))

def run_text_job(m,jid,conf,provider,prompt,size):
    job=m.jobs.get(jid)
    if not job or job.get('deleted_at') or job['status']!='processing':return
    started=time.monotonic();timings={'queue_ms':max(0,round((time.time()-job['created_at'])*1000)),'normalize_ms':0}
    temp=os.path.join(m.UPLOAD_DIR,'tmp_'+jid+'.jpg');out=os.path.join(m.UPLOAD_DIR,job['result_file'])
    try:
        m.jobs.update(jid,stage='enhance',started_at=time.time())
        phase=time.monotonic()
        OpenAIImagesEnhance(provider).generate(temp,prompt,conf['model'],size,conf['endpoint'])
        # The queued provider mapping is a snapshot, not a current price lookup.
        # Generation has completed; a later audit/storage failure still incurred
        # this reference expense, but an ambiguous/rejected submit records none.
        m.jobs.update(jid,provider='worldcodes',cost_cny=provider.get('price_light_cny',0),cost_estimated=True)
        timings['provider_ms']=round((time.monotonic()-phase)*1000);phase=time.monotonic()
        m._finalize(temp,out);width,height=m._validate_output(out)
        with open(out,'rb') as f:rejected=m._moderate_or_reject(f.read(),jid,job['openid'])
        if rejected:raise RuntimeError(rejected)
        current=m.jobs.get(jid)
        if not current or current.get('deleted_at'):raise RuntimeError('作品已删除，停止交付')
        timings['finalize_ms']=round((time.monotonic()-phase)*1000);phase=time.monotonic()
        m.jobs.update(jid,stage='store_cos')
        m.cleanup.schedule('cos',job['result_cos'],time.time()+2*m.JOB_TTL_SECONDS)
        with open(out,'rb') as f:m.cos_put(m.settings,job['result_cos'],f.read())
        if not m.cos_head(m.settings,job['result_cos']):raise RuntimeError('COS 成品确认失败')
        current=m.jobs.get(jid)
        if not current or current.get('deleted_at'):raise RuntimeError('作品已删除，停止交付')
        timings.update(storage_ms=round((time.monotonic()-phase)*1000),processing_ms=round((time.monotonic()-started)*1000))
        m.jobs.update(jid,status='succeeded',stage='done',provider='worldcodes',width=width,height=height,completed_at=time.time(),timings=timings)
        current=m.jobs.get(jid)
        if not current or current.get('deleted_at') or current['status']!='succeeded':
            raise RuntimeError('作品已删除，停止交付')
        m.users.complete_charge(jid)
        for kind,target in [('cos',job['result_cos']),('local',job['result_file'])]:m.cleanup.schedule(kind,target,time.time()+m.JOB_TTL_SECONDS)
    except Exception as exc:
        current=m.jobs.get(jid)
        if current and current.get('status')=='succeeded':
            m.log.exception('文字作品已完成，后续维护异常；保留成品：%s',jid)
            return
        m.jobs.update(jid,status='failed',error=str(exc)[:500],timings=timings)
        m.users.refund_job(job['openid'],jid)
        m._safe_remove(out);m.cleanup.schedule('cos',job['result_cos'],time.time())
    finally:m._safe_remove(temp)

def make_text_router(runtime):
    router=APIRouter()
    @router.post('/api/text-generation')
    def create_text_job(payload:TextBody,request:Request):
        m=runtime();user=m._rescue_guard(request)
        prompt=payload.prompt.strip()
        if not 1<=len(prompt)<=500:raise HTTPException(status_code=400,detail='请填写 1~500 字的画面描述')
        if payload.aspect_ratio not in SIZES:raise HTTPException(status_code=400,detail='请选择支持的画幅')
        if not ready(m):raise HTTPException(status_code=503,detail='文生图尚未启用，请先配置模型与 COS')
        conf=m.settings.text_generation();provider=text_gateway(m.settings,conf)
        rejected=m._moderate_text_or_reject(prompt,user['openid'])
        if rejected:m._reject_uploaded_content(user['openid'],'text',rejected,'light',{'price':conf['price']})
        if m.cloud.enabled():
            return m.cloud.admit(user['openid'],aspect_ratio=payload.aspect_ratio,
                text={**conf,'prompt':prompt,'size':SIZES[payload.aspect_ratio]})
        jid=uuid.uuid4().hex[:12];free=m.settings.free_mode();charged=0 if free else conf['price']
        try:balance=m.users.reserve_job(user['openid'],jid,charged,m.settings.quota(),free)
        except m.AdmissionError as exc:raise HTTPException(status_code=exc.status,detail=str(exc)) from exc
        try:
            key='results/%s/%s.jpg'%(user['openid'][:8],jid)
            m.jobs.create(jid,openid=user['openid'],input_mode='text',quality='light',template_id='',template_name='文字生图',
                price=conf['price'],charged_amount=charged,orig_file='',result_file='result_'+jid+'.jpg',result_cos=key,
                stage='queued',aspect_ratio=payload.aspect_ratio)
            m.users.confirm_job(jid,'文字生图')
            m.pool.submit(run_text_job,m,jid,dict(conf),dict(provider),prompt,SIZES[payload.aspect_ratio])
        except Exception as exc:
            m.users.refund_job(user['openid'],jid,cancel=True)
            if m.jobs.get(jid):m.jobs.update(jid,status='failed',error='任务提交失败')
            if isinstance(exc,QueueFull):raise HTTPException(status_code=429,detail=str(exc)) from exc
            raise
        return {'code':0,'job_id':jid,'status':m.jobs.get(jid)['status'],'quality':'light','balance':balance,'free_mode':free,'input_mode':'text'}
    return router
