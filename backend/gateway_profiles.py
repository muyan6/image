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


def text_gateway(settings):
    return settings.gateway_for('light')
