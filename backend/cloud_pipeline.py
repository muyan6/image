"""Durable metadata-only phases. Remote generations do not hold local worker threads.

One uvicorn process owns this scheduler. POST is never retried after an ambiguous
outcome; GET/CI operations are idempotent and can resume after a restart.
"""
import copy
import hashlib
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from fastapi import HTTPException
import cos_store as cos
from cloud_origin import MEDIA_HOST, IMPORT_PREFIX
from cloud_layout import text_rule, processing_batches
from gateway_async import AsyncImages, GatewayAsyncError, key_fingerprint, failure_diagnostic
from gateway_profiles import text_gateway
from image_processing import normalization_rule
from cloud_audit import CloudAudit
from template_share_rewards import reward_snapshot
from gateway_runtime import gateway_snapshots, resolve_gateway, selected_gateway_packet, explicitly_rejected, text_configuration_snapshot


class CloudPipeline:
    def __init__(self, runtime):
        self.runtime = runtime
        self.lock = threading.RLock()
        self.busy = set()
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.executor = None
        self.thread = None
        self.ready_cache = (None, 0, False)
        self._audits = None
        self._audit_db_path = None

    @property
    def audits(self):
        with self.lock:
            path = self.runtime().jobs._db_path
            if self._audits is None or self._audit_db_path != path:
                if self._audits is not None:
                    self._audits.close()
                self._audits = CloudAudit(self.runtime)
                self._audit_db_path = path
            return self._audits

    def config(self): return self.runtime().settings.cloud_pipeline()
    def enabled(self): return self.config()['enabled']
    def pending(self):
        return self.runtime().jobs.pending_cloud()

    def ready(self,quality='light',*,text=False):
        m = self.runtime()
        candidates=gateway_snapshots(m.settings,quality,text=text)
        if not (m.settings.cos_ready() and candidates):
            return False
        if candidates[0].get('request_mode')=='sync':return True
        if urlsplit(candidates[0]['base']).scheme!='https':return False
        return self.origin_ready()

    def origin_ready(self):
        m=self.runtime()
        tc=m.settings.tencent()
        fingerprint=hashlib.sha256(repr(sorted(tc.items())).encode()).hexdigest()
        if self.ready_cache[0] == fingerprint and time.time() < self.ready_cache[1]:
            return self.ready_cache[2]
        try: ready = cos.origin_ready(m.settings, MEDIA_HOST, IMPORT_PREFIX)
        except cos.CosError: ready = False
        self.ready_cache = (fingerprint, time.time()+60, ready)
        return ready

    def snapshot(self):
        with self.lock:
            pending=self.pending();conf=self.config()
            return {'enabled':conf['enabled'], 'generation_concurrency':conf['generation_concurrency'],
                    'metadata_workers':4, 'queued':sum(j.get('cloud_phase')=='queued' for j in pending),
                    'remote_active':sum(j.get('cloud_phase') in ('prepare','submit','submitting','unknown','generating') for j in pending),
                    'processing':len(pending), 'capacity':conf['max_queued']+conf['generation_concurrency'],
                    'image_bytes_through_backend':False}

    def start(self):
        if self.thread and self.thread.is_alive(): return
        self.stop_event.clear()
        self.executor=ThreadPoolExecutor(max_workers=4,thread_name_prefix='cloud-metadata')
        self.thread=threading.Thread(target=self.loop,name='cloud-dispatch',daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.wake_event.set()
        if self.thread: self.thread.join(timeout=2)
        if self.executor: self.executor.shutdown(wait=True, cancel_futures=True)

    def loop(self):
        while not self.stop_event.is_set():
            self.wake_event.clear()
            try:self.tick()
            except Exception:self.runtime().log.exception('云端任务调度异常')
            self.wake_event.wait(.25)

    def admit(self, openid, quality='light', style='', source=None, template=None,
              text_values=None, aspect_ratio='', custom_prompt='', text=None, expected_price=None,
              job_id=None, recipe=None):
        m=self.runtime()
        if not self.ready(quality,text=bool(text)):raise HTTPException(503,detail='云端生成通道未就绪：请核对所选生成网关配置和 COS 限定回源规则')
        template=m.select_template_quality(template,quality) if template else None
        if template:aspect_ratio=''
        if text_values:
            try:
                text_rule(1024, 1536, template or {}, dict(text_values))
            except ValueError as exc:
                raise HTTPException(400, detail=str(exc)) from exc
        if text:
            try:
                current_text=text_configuration_snapshot(m.settings,quality=quality)
                if text.get('connection_snapshot') is not None and text['connection_snapshot']!=current_text:
                    raise ValueError('文字生图配置已变更，请重新创建任务')
                # These overrides are trusted internal inputs, not TextBody
                # fields. Freeze the actual POST while comparing the original
                # configured model/path when a queued task reaches submission.
                model=text.get('model') or current_text['model']
                endpoint=text.get('endpoint') or current_text['endpoint']
                if (not isinstance(model,str) or not model.strip()
                        or not isinstance(endpoint,str) or not endpoint.startswith('/')
                        or endpoint.startswith('//') or '?' in endpoint or '#' in endpoint):
                    raise ValueError('文字生图模型或接口路径无效')
                candidates=[{**current_text,'config_model':current_text['model'],
                             'config_endpoint':current_text['endpoint'],'model':model,'endpoint':endpoint}]
            except ValueError as exc:raise HTTPException(503,detail=str(exc)) from exc
        else:candidates=gateway_snapshots(m.settings,quality)
        if not candidates:raise HTTPException(503,detail='没有启用且配置有效的生成网关')
        selected=candidates[0];model=selected['model']
        prompt=(text or {}).get('prompt') or str((template or {}).get('prompt') or m.settings.prompt_for(quality))
        if custom_prompt and not template:prompt+='\n用户修复需求：'+custom_prompt
        price=text['price'] if text else m._effective_price(quality,template)
        jid=job_id or uuid.uuid4().hex[:12];free=m.settings.free_mode();charged=0 if free else price
        if expected_price is not None and expected_price!=charged:
            raise HTTPException(409,detail='生成价格已更新，请刷新价格后重新确认')
        # Admission and queue capacity are checked together before reserving balance.
        with self.lock:
            cfg=self.config()
            if text and text.get('connection_snapshot') is not None:
                try:unchanged=text_configuration_snapshot(m.settings,quality=quality)==text['connection_snapshot']
                except ValueError:unchanged=False
                if not unchanged:raise HTTPException(503,detail='文字生图配置已变更，本次未预扣光子，请重新创建任务')
            if len(self.pending())>=cfg['max_queued']+cfg['generation_concurrency']:
                raise HTTPException(429,detail='云端任务队列已满，请稍后重试')
            deadline = time.time() + cfg['timeout_seconds']
            if source:
                # The permit's original one-hour expiry must not delete the
                # accepted input while it waits for a metadata/generation slot.
                m.cleanup.retain_until('cos', source['key'], deadline + 300)
            share_snapshot = reward_snapshot(template,m.settings.template_sharing()) if (template or {}).get('source') == 'user' else None
            share_kwargs = {'template_snapshot':share_snapshot} if share_snapshot is not None else {}
            try:balance=m.users.reserve_job(openid,jid,charged,m.settings.quota(),free,**share_kwargs)
            except m.AdmissionError as exc:raise HTTPException(exc.status,detail=str(exc)) from exc
            packet={'prompt':prompt,'model':model,'size':(text or {}).get('size',''),
                    'template':copy.deepcopy(template or {}),'text_values':dict(text_values or {}),
                    'source':dict(source or {}),'normalize_long_side':m.settings.normalize_long_side()}
            # Photon sale price and provider CNY cost are separate units. Freeze
            # the selected connection's reference price before queueing work.
            packet['gateway_candidates']=copy.deepcopy(candidates)
            packet=selected_gateway_packet(packet,selected,0)
            prefix=openid[:8]+'/'+jid
            try:
                m.jobs.create(jid,openid=openid,status='processing',stage='queued',cloud_pipeline=True,
                    cloud_phase='queued',cloud_request=packet,quality=quality,style=style,aspect_ratio=aspect_ratio,
                    provider=selected['gateway_id'],provider_name=selected['gateway_name'],
                    template_id=(template or {}).get('id',''),template_name=(template or {}).get('name','文字生图' if text else ''),
                    template_output_mode=(template or {}).get('output_mode',''),price=price,charged_amount=charged,
                    input_mode='text' if text else 'photo',recipe=recipe or (
                        {'input_mode':'text','quality':quality,'template_id':'','text_fields':{},'custom_prompt':'',
                         'aspect_ratio':aspect_ratio,'template_output_mode':'','prompt':text['prompt']} if text else
                        m.photo_recipe(quality,template,text_values,aspect_ratio,custom_prompt,(template or {}).get('output_mode',''))),
                    orig_file='',result_file='',
                    orig_cos=('origins/'+prefix+source['ext']) if source else None,
                    result_cos='results/'+prefix+'.jpg',norm_cos=('norms/'+prefix+'.jpg') if source else None,
                    deadline=deadline,cloud_next_at=0,
                    cloud_audit_mode='ci_sync' if openid.startswith('web-') else cfg.get('audit_mode','wechat_auto'),
                    **share_kwargs)
                m.users.confirm_job(jid,'云端异步生成 '+model)
            except Exception:
                m.users.refund_job(openid,jid,cancel=True)
                if m.jobs.get(jid):m.jobs.update(jid,status='failed',error='云端任务登记失败')
                raise
        self.wake_event.set()
        return {'code':0,'job_id':jid,'status':'processing','quality':quality,'balance':balance,
                'price':price,'free_mode':free,'input_mode':'text' if text else 'photo','orig_url':None,'result_url':None}

    def tick(self):
        m=self.runtime()
        with self.lock:
            pending=sorted(self.pending(),key=lambda j:j['created_at'])
            active=sum(j.get('cloud_phase') in ('prepare','submit','submitting','unknown','generating','wait_audit') for j in pending)
            for job in pending:
                if len(self.busy)>=4:break
                if job['id'] in self.busy or time.time()<job.get('cloud_next_at',0):continue
                if job.get('cloud_phase')=='queued':
                    if active>=self.config()['generation_concurrency']:continue
                    m.jobs.update(job['id'],cloud_phase='prepare',stage='normalize',started_at=time.time())
                    active+=1
                self.busy.add(job['id'])
                try:self.executor.submit(self.run_ready_steps,job['id'])
                except Exception:self.busy.discard(job['id']);raise

    def run_ready_steps(self,jid):
        """Keep ownership across immediate metadata phases; never sleep for remote work."""
        try:
            for _ in range(8):
                job=self.runtime().jobs.get(jid)
                if (not job or not job.get('cloud_pipeline') or job.get('deleted_at') or job['status']!='processing'
                        or time.time()<job.get('cloud_next_at',0)):
                    break
                phase=job['cloud_phase']
                self.step(jid,release=False)
                current=self.runtime().jobs.get(jid)
                # Unchanged phases are asynchronous waits, not a reason to poll again.
                if not current or current.get('cloud_phase')==phase:break
        finally:
            with self.lock:self.busy.discard(jid)
            self.wake_event.set()

    def require_live(self,jid):
        job=self.runtime().jobs.get(jid)
        if not job or job.get('deleted_at') or job['status']!='processing':raise RuntimeError('作品已取消，停止交付')
        return job

    def audit(self,key):
        m=self.runtime();mod=m.settings.moderation()
        if not mod.get('enabled'):return None
        try: verdict=cos.audit_object(m.settings,key,self.config()['ci_biz_type'])
        except cos.CosError as exc:
            # Switching engines must not turn a missing CI permission into an unchecked image.
            raise RuntimeError('云端安全审核服务未就绪，本次停止并退回光子') from exc
        return None if verdict['result']==0 else '图片内容未通过安全审核'

    def prepare(self,job):
        m=self.runtime();p=job['cloud_request'];source=p['source'];jid=job['id']
        if source:
            # Freeze an immutable server-owned snapshot before any asynchronous
            # audit: the client's signed upload URL can overwrite uploads/.
            if not job.get('cloud_input_frozen'):
                meta=cos.object_metadata(m.settings,source['key'])
                if not 0<meta['size']<=m.MAX_UPLOAD_BYTES:raise ValueError('图片超过上传大小上限或为空')
                m.cleanup.schedule('cos',job['orig_cos'],job['created_at']+m.ORIGINAL_TTL_SECONDS)
                cos.copy_object(m.settings,source['key'],job['orig_cos'])
                m.jobs.update(jid,cloud_input_frozen=True)
            cached=job.get('cloud_input_info')
            meta=cached['meta'] if cached else cos.object_metadata(m.settings,job['orig_cos'])
            if not 0<meta['size']<=m.MAX_UPLOAD_BYTES:raise ValueError('图片超过上传大小上限或为空')
            if source.get('byte_size',0)>0 and meta['size']!=source['byte_size']:
                raise ValueError('上传图片大小与登记信息不一致，请重新上传')
            info=cached['info'] if cached else cos.image_info(m.settings,job['orig_cos']);w,h=info['width'],info['height']
            if not cached:m.jobs.update(jid,cloud_input_info={'meta':meta,'info':info})
            if w*h>m.MAX_PIXELS:raise ValueError('图片像素过大，请先缩小')
            if job.get('cloud_audit_mode',self.config().get('audit_mode')) == 'wechat_auto':
                if not self.audits.gate(job,job['orig_cos'],meta['size'],'input'):return
                reject=None
            else:reject=self.audit(job['orig_cos'])
            if reject:
                # Refund the reservation first; only confirmed user-input violations are penalized.
                m.users.refund_job(job['openid'],jid)
                m.users.record_violation(job['openid'],jid,'image',reject,job['charged_amount'])
                raise ValueError(reject)
            self.require_live(jid)
            due=job['created_at']+m.ORIGINAL_TTL_SECONDS
            m.cleanup.schedule('cos',job['orig_cos'],due)
            try: orientation=int(info.get('Orientation',info.get('orientation',1)))
            except (ValueError,TypeError):orientation=1
            if orientation in (5,6,7,8):w,h=h,w
            m.cleanup.schedule('cos',job['norm_cos'],time.time()+2*m.JOB_TTL_SECONDS)
            cos.process_image(m.settings,job['orig_cos'],job['norm_cos'],
                              normalization_rule(w,h,p['normalize_long_side'],job.get('aspect_ratio','')))
            norm=cos.image_info(m.settings,job['norm_cos'])
            m.jobs.update(jid,comparison_cos=job['norm_cos'],source_width=norm['width'],source_height=norm['height'])
            m.cleanup.schedule('cos',source['key'],time.time())
        self.require_live(jid)
        m.jobs.update(jid,cloud_phase='submit',stage='enhance')

    def submit(self,job):
        m=self.runtime();p=job['cloud_request'];provider=resolve_gateway(m.settings,job['quality'],p,text=job.get('input_mode')=='text')
        if p.get('request_mode')=='sync' and job.get('input_mode')!='text':
            m._queue_sync_gateway_job(job['id'],p);return
        url=cos.presign(m.settings,'get',job['norm_cos'],ttl_seconds=3600) if job.get('norm_cos') else None
        size=p['size']
        if not size and p['template']:
            # Output instructions describe forbidden comparisons, not the artwork's ratio.
            # The split also supports queued snapshots created before layout_prompt existed.
            prompt=p['template'].get('layout_prompt')
            if prompt is None:prompt=p['prompt'].split('\n\n【本次成品形式：',1)[0]
            prompt=prompt.lower()
            if p['template'].get('layout') or any(word in prompt for word in ('上下','竖版','竖向','纵向','长图','portrait','vertical','top half','bottom half')):
                size='1024x1536'
        if not size:
            w,h=job.get('source_width',1),job.get('source_height',1)
            size='1536x1024' if w/h>1.15 else ('1024x1536' if w/h<.87 else '1024x1024')
        client=AsyncImages(provider)
        # This durable boundary is atomic with deletion. No network while holding the job lock.
        if not m.jobs.begin_cloud_submission(job['id']):
            self.require_live(job['id'])
            return  # Another worker already marked this paid POST; never send twice.
        task=client.submit(p['model'],p['prompt'],url,size,p['endpoint'])
        m.jobs.update(job['id'],vendor_task_id=task,cloud_phase='generating',stage='enhance',
                      provider=p.get('gateway_id') or ('text_generation' if job.get('input_mode')=='text' else 'worldcodes'),
                      provider_name=p.get('gateway_name') or ('文字生图' if job.get('input_mode')=='text' else 'worldcodes'),
                      cloud_next_at=time.time()+self.config()['poll_interval'])

    def poll(self,job):
        m=self.runtime();p=job['cloud_request'];provider=resolve_gateway(m.settings,job['quality'],p,text=job.get('input_mode')=='text',accepted=True)
        data=AsyncImages(provider).poll(job['vendor_task_id']);state=data.get('status')
        if state=='failed':
            detail=failure_diagnostic(data,provider.get('api_key',''))
            detail['task_id']=job['vendor_task_id']
            m.jobs.update(job['id'],vendor_failure=detail)
            reason=detail['message'] or '供应商未返回具体原因'
            if detail['code']:reason+=' ['+detail['code']+']'
            if detail['http_status']:reason+=' HTTP '+str(detail['http_status'])
            raise ValueError('供应商生成失败：'+reason+'；本次光子退回')
        if state not in ('completed','succeeded'):
            m.jobs.update(job['id'],cloud_next_at=time.time()+self.config()['poll_interval']);return
        completed={'provider':p.get('gateway_id') or ('text_generation' if job.get('input_mode')=='text' else 'worldcodes'),
                   'provider_name':p.get('gateway_name') or ('文字生图' if job.get('input_mode')=='text' else 'worldcodes'),
                   'provider_completed_at':job.get('provider_completed_at') or time.time()}
        if 'estimated_cost_cny' in p and job.get('cost_cny') is None:
            # Completion confirms generation, not delivery. Retain this clearly
            # estimated expense even if import, geometry or output audit fails.
            completed.update(cost_cny=p['estimated_cost_cny'],cost_estimated=True)
        m.jobs.update(job['id'],**completed)
        result=data.get('result') or {};items=result.get('data') or []
        if len(items)!=1 or not isinstance(items[0],dict) or not items[0].get('url'):
            raise ValueError('供应商未返回单张云端图片地址')
        key=cos.mirror_key(items[0]['url'],MEDIA_HOST,IMPORT_PREFIX)
        m.jobs.update(job['id'],cloud_phase='import',vendor_result_key=key,stage='store_cos',cloud_next_at=0)

    def import_result(self,job):
        from cloud_import import import_result
        import_result(self,job)

    def finalize(self,job):
        m=self.runtime();p=job['cloud_request'];key=job['vendor_result_key']
        info=job.get('cloud_import_info') or cos.image_info(m.settings,key);w,h=info['width'],info['height']
        aspect='' if p['template'] or job['input_mode']=='text' else f"{job['source_width']}:{job['source_height']}"
        rule=normalization_rule(w,h,0,aspect).replace('/quality/85','/quality/95')
        if p['text_values']:
            # Text is rendered by CI, not by the AI model or the VM.
            overlay=text_rule(w,h,p['template'],p['text_values'])
            if overlay:rule+='|'+overlay
        self.require_live(job['id'])
        m.cleanup.schedule('cos',job['result_cos'],time.time()+2*m.JOB_TTL_SECONDS)
        if not job.get('cloud_output_prepared'):
            batches=processing_batches(rule)
            stem,ext=job['result_cos'].rsplit('.',1)
            intermediate=[f'{stem}_layout_{i+1:03d}.{ext}' for i in range(len(batches)-1)]
            targets=intermediate+[job['result_cos']]
            m.jobs.update(job['id'],cloud_layout_keys=intermediate)
            for target in intermediate:
                m.cleanup.schedule('cos',target,job['deadline']+3600)
            done=int(job.get('cloud_output_batches_done') or 0)
            prepared_meta=None
            for index in range(done,len(batches)):
                self.require_live(job['id'])
                source=key if index==0 else targets[index-1]
                prepared_meta=cos.process_image(m.settings,source,targets[index],batches[index])
                # Acknowledged batches survive retries/restarts. Each target is
                # independent, so replaying an unacknowledged batch is idempotent.
                m.jobs.update(job['id'],cloud_output_batches_done=index+1)
            if prepared_meta is None:
                prepared_meta=cos.object_metadata(m.settings,job['result_cos'])
            final=cos.image_info(m.settings,job['result_cos'])
            m.jobs.update(job['id'],cloud_output_prepared=True,cloud_output_info=final,cloud_output_meta=prepared_meta)
            for target in intermediate:m.cleanup.schedule('cos',target,time.time())
        else:final=job.get('cloud_output_info') or cos.image_info(m.settings,job['result_cos'])
        meta=job.get('cloud_output_meta') if job.get('cloud_output_prepared') else prepared_meta
        if not isinstance(meta,dict):meta=cos.object_metadata(m.settings,job['result_cos'])
        if meta['size']<=0:raise ValueError('云端成品为空')
        if job.get('cloud_audit_mode',self.config().get('audit_mode')) == 'wechat_auto':
            if not self.audits.gate(job,job['result_cos'],meta['size'],'output'):return
            reject=None
        else:reject=self.audit(job['result_cos'])
        if reject:raise ValueError(reject)  # Output violations refund; no user strikes.
        self.require_live(job['id']);now=time.time()
        timings={**(m.jobs.get(job['id']).get('timings') or {}),'queue_ms':round((job.get('started_at',now)-job['created_at'])*1000),
                 'provider_ms':round((job.get('provider_completed_at',now)-job.get('submitted_at',now))*1000),
                 'processing_ms':round((now-job.get('started_at',now))*1000)}
        m.jobs.update(job['id'],status='succeeded',stage='done',cloud_phase='done',
                      provider=p.get('gateway_id') or ('text_generation' if job.get('input_mode')=='text' else 'worldcodes'),
                      provider_name=p.get('gateway_name') or ('文字生图' if job.get('input_mode')=='text' else 'worldcodes'),
                      width=final['width'],height=final['height'],completed_at=now,timings=timings,processing_mode='cloud_only')
        if 'estimated_cost_cny' in p and job.get('cost_cny') is None:
            m.jobs.update(job['id'],cost_cny=p['estimated_cost_cny'],cost_estimated=True)
        current=m.jobs.get(job['id'])
        if current.get('deleted_at') or current['status']!='succeeded':raise RuntimeError('作品已取消，停止交付')
        m.users.complete_charge(job['id'])
        for key in (job.get('orig_cos'),job.get('result_cos'),job.get('norm_cos')):
            if key:
                try:m._retain_job_object(job['id'],'cos',key,now+m.JOB_TTL_SECONDS)
                except Exception:
                    # The periodic retention sweep retries this maintenance operation.
                    m.log.exception('已完成作品的留存信息待后台重试：%s',job['id'])
        if job.get('template_id'):
            try:m.templates.inc_usage(job['template_id'])
            except Exception:m.log.exception('模板热度写入失败')

    def retry_cloud_step(self,job,phase,exc):
        # Only idempotent COS steps are retried; never the paid supplier POST.
        if phase not in ('prepare','finalize') or not isinstance(exc,cos.CosError):return False
        if exc.status not in (408,429,500,502,503,504) and exc.code!='NETWORK':return False
        now=time.time();counts=dict(job.get('phase_retry_counts') or {});attempt=counts.get(phase,0)+1
        if attempt>3 or now>=job['deadline']:return False
        counts[phase]=attempt
        self.runtime().jobs.update(job['id'],phase_retry_counts=counts,last_phase_error=str(exc)[:300],
                                   cloud_next_at=min(job['deadline'],now+min(15,2**attempt)))
        return True

    def fail(self,job,message):
        m=self.runtime()
        if not m.jobs.fail_cloud_job(job['id'],stage='failed',cloud_phase='failed',error=message[:500],
                      failed_phase=job.get('cloud_phase'),failed_stage=job.get('stage'),failed_at=time.time()):
            return
        m.users.refund_job(job['openid'],job['id'])
        for key in (job.get('result_cos'),job.get('norm_cos'),job.get('orig_cos'),job['cloud_request']['source'].get('key'),*(job.get('cloud_layout_keys') or [])):
            if key:m.cleanup.schedule('cos',key,time.time())

    def advance_rejected(self,job,error):
        """Never advance unknown/accepted jobs; only explicit unaccepted HTTP responses."""
        if job.get('input_mode')=='text' or job.get('vendor_task_id') or not explicitly_rejected(error):return False
        packet=job['cloud_request'];candidates=packet.get('gateway_candidates') or []
        index=int(packet.get('gateway_index',0))+1
        if index>=len(candidates):return False
        next_packet=selected_gateway_packet(packet,candidates[index],index)
        # A disabled/changed queued candidate fails rather than borrowing another live host.
        resolve_gateway(self.runtime().settings,job['quality'],next_packet)
        return self.runtime().jobs.advance_rejected_gateway(job['id'],packet.get('gateway_id','worldcodes'),next_packet,error.status)

    def wait_audit(self,job):
        self.audits.wait(job)

    def step(self,jid,*,release=True):
        m=self.runtime();job=m.jobs.get(jid);phase=(job or {}).get('cloud_phase','unknown');started=time.monotonic()
        try:
            job=self.require_live(jid)
            if time.time()>job['deadline']:raise ValueError('云端任务超时，本次光子退回；提交状态不明时不重复调用供应商')
            phase=job['cloud_phase']
            if phase in ('submitting','unknown'):
                m.jobs.update(jid,cloud_phase='unknown',stage='submission_unknown',cloud_next_at=time.time()+30)
            else:getattr(self,{'prepare':'prepare','submit':'submit','generating':'poll','import':'import_result','finalize':'finalize','wait_audit':'wait_audit'}[phase])(job)
        except GatewayAsyncError as exc:
            current=m.jobs.get(jid)
            if phase=='submit' and explicitly_rejected(exc):
                refused=m.jobs.clear_cloud_rejection(jid,(job.get('cloud_request') or {}).get('gateway_id','worldcodes'),exc.status)
                if refused and refused.get('deleted_at'):
                    m.users.refund_job(refused['openid'],jid)
                    current=refused
            if not current or current.get('deleted_at') or current.get('status')!='processing':
                pass  # A late vendor response cannot refund or revive a cancelled job.
            elif exc.uncertain:
                m.jobs.update(jid,cloud_phase='unknown',stage='submission_unknown',cloud_next_at=time.time()+30)
            elif job and job.get('cloud_phase')=='generating' and exc.retryable:
                m.jobs.update(jid,cloud_next_at=time.time()+15,last_poll_error=str(exc)[:200])
            elif phase=='submit' and explicitly_rejected(exc):
                try:advanced=self.advance_rejected(current,exc)
                except ValueError as changed:
                    self.fail(current,str(changed));advanced=True
                if not advanced:self.fail(current,str(exc))
            elif job:
                m.jobs.update(jid,last_poll_error=str(exc)[:200],vendor_http_status=exc.status)
                self.fail(job,str(exc))
        except Exception as exc:
            current=m.jobs.get(jid)
            if current and current.get('status')=='processing' and not current.get('deleted_at'):
                if not self.retry_cloud_step(current,phase,exc):
                    self.fail(current,str(exc) if isinstance(exc,(ValueError,cos.CosError,RuntimeError)) else '云端处理未完成，本次光子退回')
            elif current and (current.get('deleted_at') or current.get('status')=='failed'):
                # A delete may race a cloud copy; delete unique targets after that copy finishes.
                for key in (current.get('result_cos'),current.get('norm_cos'),current.get('orig_cos'),*(current.get('cloud_layout_keys') or [])):
                    if key:m.cleanup.schedule('cos',key,time.time())
                if current.get('deleted_at'):
                    m.cleanup.delete_cos_now(m.settings,m._job_cos_keys(current))
            elif current and current.get('status')=='succeeded':
                m.log.exception('作品已完成，后续维护异常；保留成品：%s',jid)
        finally:
            current=m.jobs.get(jid)
            if current and phase!='wait_audit':
                timings=dict(current.get('timings') or {})
                name=phase+'_ms';timings[name]=timings.get(name,0)+round((time.monotonic()-started)*1000)
                m.jobs.update(jid,timings=timings)
            if release:
                with self.lock:self.busy.discard(jid)
