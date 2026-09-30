"""Small JSON-only gateway client; exactly one submit, no full image response."""
import hashlib
import re
from urllib.parse import urlsplit
import requests

class GatewayAsyncError(RuntimeError):
    def __init__(self,message,uncertain=False):super().__init__(message);self.uncertain=uncertain

def key_fingerprint(conf):return hashlib.sha256(str(conf.get('api_key') or '').encode()).hexdigest()

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
            response=requests.request(method,self.base+path,
                headers={'Authorization':'Bearer '+self.conf['api_key'],'Content-Type':'application/json'},
                json=body,timeout=(5,30),allow_redirects=False,stream=True)
            with response:
                chunks=[];count=0
                for chunk in response.iter_content(8192):
                    count+=len(chunk)
                    if count>256*1024:raise GatewayAsyncError('异步接口返回超过元数据上限',uncertain=method=='POST')
                    chunks.append(chunk)
                import json
                data=json.loads(b''.join(chunks))
                if not isinstance(data,dict):raise GatewayAsyncError('供应商未返回对象元数据',uncertain=method=='POST')
                if response.status_code>=400:
                    error=data.get('error') or {}
                    if not isinstance(error,dict):error={}
                    code=error.get('code') or error.get('type') or str(response.status_code)
                    code=str(code).replace(self.conf['api_key'],'[redacted]')[:100]
                    raise GatewayAsyncError('供应商异步接口拒绝请求：'+code,uncertain=method=='POST' and response.status_code>=500)
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
