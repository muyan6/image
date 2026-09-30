"""Bounded COS-only import wait; never resubmit AI or fetch full image bytes."""
import time
import cos_store as cos

WAIT_SECONDS=120
TRIGGER_LIMIT=5
TRANSIENT_STATUS=(404,408,424,429,500,502,503,504)


def import_result(pipeline,job):
    m=pipeline.runtime();jid=job['id'];key=job['vendor_result_key'];now=time.time()
    started=job.get('cloud_import_started_at') or now
    deadline=min(job['deadline'],started+WAIT_SECONDS)
    m.jobs.update(jid,cloud_import_started_at=started,provider='worldcodes',stage='store_cos')
    if now>=deadline:
        raise RuntimeError('COS 成品导入等待超时；'+(job.get('import_last_error') or '对象尚未落盘'))
    m.cleanup.schedule('cos',key,job['deadline']+3600)
    attempts=job.get('import_trigger_attempts',0)
    try:
        # Existing cached imports need no GET. An absent object is not an AI failure.
        try:meta=cos.object_metadata(m.settings,key)
        except cos.CosError as exc:
            if exc.status!=404:raise
            if not job.get('cloud_import_triggered'):
                if attempts>=TRIGGER_LIMIT:
                    raise RuntimeError('COS 成品回源连续失败；'+(job.get('import_last_error') or str(exc)))
                attempts+=1;m.jobs.update(jid,import_trigger_attempts=attempts)
                cos.trigger_mirror(m.settings,key)
                m.jobs.update(jid,cloud_import_triggered=True)
            meta=cos.object_metadata(m.settings,key)
        if not 0<meta['size']<=32*1024*1024:raise ValueError('云端结果大小异常')
        info=cos.image_info(m.settings,key)
        if info['width']*info['height']>m.MAX_PIXELS:raise ValueError('云端结果像素过大')
        pipeline.require_live(jid)
        timings=dict(m.jobs.get(jid).get('timings') or {})
        timings['import_wait_ms']=round((time.time()-started)*1000)
        m.jobs.update(jid,cloud_phase='finalize',stage='finalize',cloud_import_info=info,
                      import_last_error='',import_diagnostic={},timings=timings,cloud_next_at=0)
    except cos.CosError as exc:
        m.jobs.update(jid,import_last_error=str(exc)[:500],import_diagnostic=exc.details)
        retry=exc.status in TRANSIENT_STATUS or exc.code=='NETWORK'
        if not retry:raise
        if time.time()>=deadline or (attempts>=TRIGGER_LIMIT and not m.jobs.get(jid).get('cloud_import_triggered')):
            raise RuntimeError('COS 成品导入失败；'+str(exc)) from exc
        pipeline.require_live(jid)
        m.jobs.update(jid,cloud_phase='import',stage='store_cos',
                      cloud_next_at=min(deadline,time.time()+min(15,2**min(attempts+1,4))))
