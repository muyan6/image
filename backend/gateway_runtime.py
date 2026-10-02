"""Stable gateway admission evidence. Credentials remain in settings, never in jobs."""
import copy
from urllib.parse import urlsplit
from gateway_async import key_fingerprint
from gateway_profiles import text_gateway

CLEAR_REJECTION_STATUSES = frozenset((400,401,403,404,405,415,422,429))

def explicitly_rejected(error):
    return not getattr(error,'uncertain',False) and getattr(error,'status',None) in CLEAR_REJECTION_STATUSES

def snapshot_gateway(conf,quality='light',*,text=False,https=False):
    model=conf.get('model_'+quality)
    base=str(conf.get('base_url') or '').strip().rstrip('/')
    url=urlsplit(base)
    if not (conf.get('enabled') and conf.get('api_key') and model and url.hostname
            and url.scheme in (('https',) if https else ('http','https'))
            and not url.username and not url.password and not url.query and not url.fragment):
        raise ValueError('生成网关未配置完整或地址无效')
    return {'gateway_id':'text_generation' if text else conf['gateway_id'],
            'gateway_name':'文字生图' if text else conf['gateway_name'],
            'base':base,'endpoint':conf.get('endpoint') or '/v1/images/edits',
            'request_mode':conf.get('request_mode') or ('async' if text or conf.get('gateway_id')=='worldcodes' else 'sync'),
            'async_endpoint':conf.get('async_endpoint') or '',
            'model':model,'key_fingerprint':key_fingerprint(conf),
            'estimated_cost_cny':conf.get('price_'+quality+'_cny',0)}

def gateway_snapshots(settings,quality='light',*,text=False,https=False):
    configs=[{**text_gateway(settings),'gateway_id':'text_generation','gateway_name':'文字生图'}] if text else settings.gateway_candidates(quality)
    result=[]
    for conf in configs:
        try:result.append(snapshot_gateway(conf,quality,text=text,https=https))
        except ValueError:continue
    return result


def text_configuration_snapshot(settings,conf=None,quality='light'):
    """One detached independent connection/fee binding, containing no raw key."""
    conf=settings.text_generation() if conf is None else conf
    provider=text_gateway(settings,conf)
    result=snapshot_gateway(provider,quality,text=True)
    result.update(credit_price=conf['price'],request_timeout=provider['request_timeout'])
    return result

def resolve_gateway(settings,quality,snapshot,*,text=False,accepted=False):
    """Explicit ID lookup ignores new priority; only accepted tasks ignore enable/model edits."""
    gid=snapshot.get('gateway_id') or ('text_generation' if text else 'worldcodes')
    conf=text_gateway(settings) if text else settings.gateway_for(quality,gateway_id=gid)
    base=str(conf.get('base_url') or '').strip().rstrip('/')
    if not conf or base!=snapshot.get('base') or key_fingerprint(conf)!=snapshot.get('key_fingerprint'):
        raise ValueError('原生成网关地址或凭据已变更，本次停止并核对任务')
    if not accepted and (not conf.get('enabled') or (conf.get('endpoint') or '/v1/images/edits')!=snapshot.get('config_endpoint',snapshot.get('endpoint'))
                         or conf.get('model_'+quality)!=snapshot.get('config_model',snapshot.get('model'))
                         or 'request_mode' in snapshot and conf.get('request_mode','async' if text or gid=='worldcodes' else 'sync')!=snapshot['request_mode']
                         or 'async_endpoint' in snapshot and (conf.get('async_endpoint') or '')!=snapshot['async_endpoint']):
        raise ValueError('原生成网关配置已变更，本次未提交生成，请重新创建任务')
    result=copy.deepcopy(conf)
    result.update(gateway_id=gid,gateway_name=snapshot.get('gateway_name') or gid,
                  base_url=snapshot['base'],endpoint=snapshot['endpoint'])
    if 'request_mode' in snapshot:result['request_mode']=snapshot['request_mode']
    if 'async_endpoint' in snapshot:result['async_endpoint']=snapshot['async_endpoint']
    result['model_'+quality]=snapshot['model']
    if 'estimated_cost_cny' in snapshot:result['price_'+quality+'_cny']=snapshot['estimated_cost_cny']
    return result

def selected_gateway_packet(packet,snapshot,index):
    result=copy.deepcopy(packet);result.update(copy.deepcopy(snapshot));result['gateway_index']=index
    return result
