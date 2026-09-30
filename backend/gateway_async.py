"""Small JSON-only gateway client; exactly one submit, no full image response."""
import hashlib
import re
from urllib.parse import urlsplit
import requests
import http_transport

class GatewayAsyncError(RuntimeError):
    def __init__(self,message,uncertain=False,*,status=None):
        super().__init__(message);self.uncertain=uncertain;self.status=status
        self.retryable=status is None or status in (408,429,500,502,503,504)

def key_fingerprint(conf):return hashlib.sha256(str(conf.get('api_key') or '').encode()).hexdigest()


def failure_diagnostic(data, api_key=''):
    """Keep only bounded, redacted failure fields, not raw supplier responses."""
    result=data.get('result') if isinstance(data.get('result'),dict) else {}
    errors=[data.get('error'),result.get('error')]
    objects=[e for e in errors if isinstance(e,dict)]
    def clean(value,limit=300):
        if not isinstance(value,(str,int)):return ''
        value=str(value)
        if api_key:value=value.replace(api_key,'[redacted]')
        value=re.sub(r'https?://[^\s<>"\']+','[URL]',value,flags=re.I)
        value=re.sub(r'\bBearer\s+[^\s,;]+','Bearer [redacted]',value,flags=re.I)
        value=re.sub(r'\bsk-[A-Za-z0-9_-]{8,}','[redacted]',value)
        value=re.sub(r'((?:api[_ -]?key|token|secret|authorization)\s*[:=]\s*)[^\s,;]+',r'\1[redacted]',value,flags=re.I)
        return re.sub(r'[\x00-\x1f\x7f]',' ',value)[:limit]
    code=next((e.get('code') or e.get('type') for e in objects if e.get('code') or e.get('type')),data.get('error_code',''))
    message=next((e.get('message') for e in objects if isinstance(e.get('message'),str) and e.get('message')),None)
    if message is None:message=next((e for e in errors if isinstance(e,str) and e),None)
    message=message or data.get('message') or result.get('message') or result.get('detail') or ''
    code=clean(code,80);message=clean(message)
    status=data.get('http_status',result.get('http_status'))
    if type(status) is not int or not 100<=status<=599:status=None
    request_id=data.get('request_id') or next((e.get('request_id') for e in objects if e.get('request_id')),'')
    return {'state':'failed','code':code,'message':message,'http_status':status,
            'request_id':clean(request_id,128),'reason_available':bool(code or message)}

class AsyncImages:
    def __init__(self,conf):
        self.conf=conf;self.base=conf['base_url'].rstrip('/')
        url=urlsplit(self.base)
        if url.scheme!='https' or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise GatewayAsyncError('异步供应商地址必须为无凭据的 HTTPS 地址')
    def request(self,method,path,body=None):
        if not path.startswith('/') or path.startswith('//') or '?' in path or '#' in path:
            raise GatewayAsyncError('异步接口路径无效')
        try:
            response=http_transport.request(method,self.base+path,
                headers={'Authorization':'Bearer '+self.conf['api_key'],'Content-Type':'application/json'},
                json=body,timeout=(5,self.conf.get('request_timeout',30)),allow_redirects=False,stream=True)
            with response:
                chunks=[];count=0
                for chunk in response.iter_content(8192):
                    count+=len(chunk)
                    if count>256*1024:raise GatewayAsyncError('异步接口返回超过元数据上限',uncertain=method=='POST')
                    chunks.append(chunk)
                import json
                if response.status_code>=400:
                    raise GatewayAsyncError('供应商异步接口拒绝请求：HTTP '+str(response.status_code),
                        uncertain=method=='POST' and response.status_code>=500,status=response.status_code)
                data=json.loads(b''.join(chunks))
                if not isinstance(data,dict):raise GatewayAsyncError('供应商未返回对象元数据',uncertain=method=='POST')
                return response.status_code,data
        except requests.RequestException:raise GatewayAsyncError('供应商请求结果待核对',uncertain=method=='POST')
        except (ValueError,TypeError):raise GatewayAsyncError('供应商未返回有效元数据',uncertain=method=='POST')
    def submit(self,model,prompt,image_url=None,size=None,endpoint=None):
        path=endpoint or self.conf.get('endpoint') or '/v1/images/edits'
        path=path.rstrip('/')+'/async'
        body={'model':model,'prompt':prompt,'n':1}
        if image_url:body['images']=[{'image_url':image_url}]
        if size and size!='auto':body['size']=size
        http,data=self.request('POST',path,body)
        task=data.get('task_id') or data.get('id')
        if http!=202 or not isinstance(task,str) or not re.fullmatch(r'imgtask_[A-Za-z0-9_-]{1,80}',task):
            raise GatewayAsyncError('异步任务编号缺失，提交结果待核对',uncertain=True)
        return task
    def poll(self,task):
        if not isinstance(task,str) or not re.fullmatch(r'imgtask_[A-Za-z0-9_-]{1,80}',task):raise GatewayAsyncError('Invalid task ID')
        return self.request('GET','/v1/images/tasks/'+task)[1]
