"""Pure configuration rules for stable-ID, named OpenAI-compatible gateways."""
import copy
import math
import re
from urllib.parse import urlsplit

GATEWAY_ID = re.compile(r'^(?:worldcodes|gw_[0-9a-f]{32})$')
RETIRED_PROVIDERS = frozenset(('fal','baidu','local'))
MAX_GATEWAYS = 20
GATEWAY_DEFAULTS = {'name':'中转网关','enabled':True,'base_url':'','api_key':'',
    'endpoint':'/v1/images/edits','request_mode':'sync','async_endpoint':'','model_light':'','model_fine':'',
    'price_light_cny':0,'price_fine_cny':0,'timeout':180,'tiers':{'light':{},'fine':{}}}


def gateway_defaults(gateway_id):
    result = copy.deepcopy(GATEWAY_DEFAULTS)
    if gateway_id == 'worldcodes':result.update(name='主网关',request_mode='async')
    return result


def validate_url(value):
    if not isinstance(value,str) or len(value)>500:raise ValueError('网关地址必须是最多500字符的文本')
    if not value:return
    try:
        url=urlsplit(value);port=url.port
        if (url.scheme not in ('http','https') or not url.hostname or url.username is not None or url.password is not None
                or url.query or url.fragment or port==0 or '\\' in value
                or any(c.isspace() or ord(c)<32 for c in value) or url.netloc.endswith(':')):raise ValueError()
    except ValueError:raise ValueError('网关地址必须是有效HTTP(S)地址，不含账号、密钥、查询参数或片段') from None


def validate_endpoint(value):
    if not isinstance(value,str) or len(value)>200:raise ValueError('网关接口必须是最多200字符的文本')
    if value and (not value.startswith('/') or value.startswith('//') or '?' in value or '#' in value or '\\' in value
                  or any(c.isspace() or ord(c)<32 for c in value)):
        raise ValueError('网关接口必须是单斜杠开头的路径，不含查询参数')


def _fields(conf,tier=False):
    fields=('base_url','api_key','endpoint','async_endpoint','model') if tier else ('base_url','api_key','endpoint','async_endpoint','model_light','model_fine')
    for field in fields:
        if tier and field not in conf:continue
        value=conf.get(field,'')
        maximum=8192 if field=='api_key' else 500 if field=='base_url' else 200
        if not isinstance(value,str) or len(value)>maximum or any(ord(c)<32 for c in value):
            raise ValueError('网关字段 '+field+' 类型、长度或字符无效')
    if not tier or 'base_url' in conf:validate_url(conf.get('base_url',''))
    if not tier or 'endpoint' in conf:validate_endpoint(conf.get('endpoint',''))
    if not tier or 'async_endpoint' in conf:validate_endpoint(conf.get('async_endpoint',''))
    if not tier or 'request_mode' in conf:
        if conf.get('request_mode') not in ('async','sync'):raise ValueError('网关调用模式必须是async或sync')
    if not tier or 'timeout' in conf:
        if type(conf.get('timeout')) is not int or not 10<=conf['timeout']<=600:raise ValueError('网关超时必须是10~600秒整数')
    costs=('price_cny',) if tier else ('price_light_cny','price_fine_cny')
    for field in costs:
        if tier and field not in conf:continue
        value=conf.get(field,0)
        if type(value) not in (int,float) or not 0<=value<=1000 or not math.isfinite(value):
            raise ValueError('网关参考成本必须是0~1000的有限数字')
    if not tier or 'enabled' in conf:
        if type(conf.get('enabled')) is not bool:raise ValueError('网关启停状态必须是布尔值')


def validate_registry(providers,chain):
    if not isinstance(providers,dict) or not 1<=len(providers)<=MAX_GATEWAYS:
        raise ValueError('至少保留1个中转网关，最多20个')
    for gateway_id,conf in providers.items():
        if not isinstance(gateway_id,str) or not GATEWAY_ID.fullmatch(gateway_id):raise ValueError('网关编号无效')
        if not isinstance(conf,dict):raise ValueError('网关配置必须是对象')
        name=conf.get('name')
        if not isinstance(name,str) or not 1<=len(name.strip())<=40 or any(ord(c)<32 for c in name):
            raise ValueError('网关名称必须是1~40字符的文本')
        _fields(conf)
        tiers=conf.get('tiers',{})
        if not isinstance(tiers,dict) or any(q not in ('light','fine') for q in tiers):raise ValueError('网关档位只能是light/fine')
        for profile in tiers.values():
            if not isinstance(profile,dict):raise ValueError('档位配置必须是对象')
            _fields(profile,tier=True)
    if (not isinstance(chain,list) or len(chain)!=len(providers) or any(not isinstance(g,str) for g in chain)
            or len(set(chain))!=len(chain) or set(chain)!=set(providers)):
        raise ValueError('优先级列表必须恰好包含全部网关编号，不能留空或重复')


def profile_ready(conf,quality):
    return bool(conf and conf.get('enabled') and str(conf.get('base_url') or '').strip()
                and str(conf.get('api_key') or '').strip() and str(conf.get('model_'+quality) or '').strip())
