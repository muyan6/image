"""Independent tier connections, with read-only fallback for pre-existing settings."""
import copy


def gateway_profile(shared, quality='light'):
    if quality not in ('light','fine'):raise ValueError('生成档位无效')
    conf=copy.deepcopy(shared or {})
    profiles=conf.pop('tiers',{}) or {}
    tier=profiles.get(quality,{})
    # Missing values inherit for old configurations; explicit empty credentials do not.
    for key in ('base_url','api_key','endpoint','timeout'):
        if key in tier:conf[key]=tier[key]
    if 'model' in tier:conf['model_'+quality]=tier['model']
    if 'price_cny' in tier:conf['price_'+quality+'_cny']=tier['price_cny']
    return conf


def tier_fields(shared, quality):
    conf=gateway_profile(shared,quality)
    return {**{key:conf.get(key,'') for key in ('base_url','api_key','endpoint','timeout')},
            'model':conf.get('model_'+quality,''),'price_cny':conf.get('price_'+quality+'_cny',0)}


def text_gateway(settings, text=None):
    text=settings.text_generation() if text is None else text
    return {'enabled':text['enabled'],'base_url':text.get('base_url',''),'api_key':text.get('api_key',''),
            'endpoint':text['endpoint'],'timeout':text.get('timeout',30),'request_timeout':text.get('timeout',30),
            'model_light':text['model'],'model_fine':text['model'],
            'price_light_cny':text.get('price_cny',0),'price_fine_cny':text.get('price_cny',0)}


def materialize_text_gateway(doc, original):
    """Snapshot the old light connection once; explicit blanks never inherit.

    A partly explicit text connection does not borrow a light key for another
    host. Missing fields there get neutral defaults instead.
    """
    original=original if isinstance(original,dict) else {}
    legacy=not any(k in original for k in ('base_url','api_key'))
    light=gateway_profile(doc.get('providers',{}).get('worldcodes',{}),'light') if legacy else {}
    defaults={'base_url':light.get('base_url',''),'api_key':light.get('api_key',''),
              'timeout':30,'price_cny':light.get('price_light_cny',0)}
    changed=False
    for key,value in defaults.items():
        if key not in original:doc['text_generation'][key]=value;changed=True
    return changed
