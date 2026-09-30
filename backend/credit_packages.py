"""Server-managed offers; the released client keeps its existing catalog/checkout API."""
import copy
import hashlib
import json
import re
import time
from decimal import Decimal, InvalidOperation

POINTS_PER_YUAN = 100
GENERATION_COST = 40
MAX_AMOUNT_FEN = 100_000_000
PACKAGES = (
    {"id": "points_600", "yuan": 6, "points": 600, "bonus": 0},
    {"id": "points_3300", "yuan": 30, "points": 3300, "bonus": 300},
    {"id": "points_8160", "yuan": 68, "points": 8160, "bonus": 1360},
    {"id": "points_16640", "yuan": 128, "points": 16640, "bonus": 3840},
)


def default_commerce():
    return {"welcome_points": 100, "packages": [
        {"id": p['id'], "product_id": p['id'], "enabled": True,
         "amount_fen": p['yuan'] * 100, "points": p['points'], "description": "",
         "promotion": {"enabled": False, "starts_at": 0, "ends_at": 0,
                       "product_id": p['id'], "amount_fen": p['yuan'] * 100,
                       "points": p['points'], "description": ""}}
        for p in PACKAGES]}


def validate_commerce(doc):
    if not isinstance(doc, dict):
        raise ValueError('光子运营配置必须是对象')
    if type(doc.get('welcome_points')) is not int or not 0 <= doc['welcome_points'] <= 100000:
        raise ValueError('新用户赠送必须是 0~100000 的整数光子')
    packages = doc.get('packages')
    if not isinstance(packages, list) or len(packages) > 20:
        raise ValueError('充值套餐必须是数组，最多 20 个')
    ids = set()
    product_prices = {}
    for item in packages:
        if (not isinstance(item, dict) or not isinstance(item.get('id'), str)
                or not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', item['id'])):
            raise ValueError('套餐标识只能包含 1~40 位字母、数字、下划线和连字符')
        if item['id'] in ids:
            raise ValueError('套餐标识重复')
        ids.add(item['id'])
        if type(item.get('enabled')) is not bool:
            raise ValueError('套餐启用状态必须为布尔值')
        promo = item.get('promotion')
        if not isinstance(promo, dict) or type(promo.get('enabled')) is not bool:
            raise ValueError('活动配置或启用状态无效')
        for row in (item, promo):
            if not isinstance(row.get('product_id'), str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', row['product_id']):
                raise ValueError('请填写有效的微信商品 ID（1~64 位字母、数字、下划线或连字符）')
            if type(row.get('amount_fen')) is not int or not 1 <= row['amount_fen'] <= MAX_AMOUNT_FEN:
                raise ValueError('套餐价格必须为 0.01~1000000 元，最多两位小数')
            if type(row.get('points')) is not int or not 1 <= row['points'] <= 10000000:
                raise ValueError('到账光子必须是 1~10000000 的整数（包含赠送）')
            if not isinstance(row.get('description'), str) or len(row['description']) > 100:
                raise ValueError('套餐说明最多 100 字')
        for field in ('starts_at', 'ends_at'):
            if type(promo.get(field)) is not int or not 0 <= promo[field] <= 4102444800:
                raise ValueError('活动时间无效')
        if promo['enabled']:
            if not 0 < promo['starts_at'] < promo['ends_at']:
                raise ValueError('活动必须设置开始和结束时间，结束时间应晚于开始时间')
            if promo['amount_fen'] > item['amount_fen'] or promo['points'] < item['points']:
                raise ValueError('活动价格应不高于常规价，到账光子应不少于常规套餐')
            if promo['amount_fen'] != item['amount_fen'] and promo['product_id'] == item['product_id']:
                raise ValueError('不同售价的限时活动请使用独立微信商品 ID，以便到期自动恢复常规商品')
        if item['enabled']:
            for row in [item] + ([promo] if promo['enabled'] else []):
                previous = product_prices.setdefault(row['product_id'], row['amount_fen'])
                if previous != row['amount_fen']:
                    raise ValueError('同一微信商品 ID 不能配置不同售价')


def amount_fen(package):
    """Legacy callers may still supply yuan; never send fractional cents to WeChat."""
    if 'amount_fen' in package:
        value = package['amount_fen']
    else:
        try:
            raw = Decimal(str(package['yuan'])) * 100
            if not raw.is_finite() or raw != raw.to_integral_value():
                raise ValueError('套餐金额必须精确到分')
            value = int(raw)
        except (KeyError, InvalidOperation, TypeError, OverflowError) as exc:
            raise ValueError('套餐金额无效') from exc
    if type(value) is not int or not 1 <= value <= MAX_AMOUNT_FEN:
        raise ValueError('套餐金额超出范围')
    return value


def price_text(fen):
    whole, remainder = divmod(fen, 100)
    return '¥' + str(whole) + (('.%02d' % remainder).rstrip('0') if remainder else '')


def active_offers(catalog=None, now=None):
    catalog = default_commerce()['packages'] if catalog is None else catalog
    now = time.time() if now is None else now
    offers = []
    for item in catalog:
        if not item['enabled']:
            continue
        promo = item['promotion']
        active = promo['enabled'] and promo['starts_at'] <= now < promo['ends_at']
        row = promo if active else item
        amount, points = row['amount_fen'], row['points']
        bonus = max(0, points - amount)
        description = row['description'].strip() or (
            '多送 %d 光子' % bonus if bonus else ('标准 1 元 = 100 光子' if points == amount else '到账 %d 光子' % points))
        if active and not row['description'].strip():
            description = '限时优惠 · ' + description
        # The ID is an offer version, not the WeChat product ID. Old pages must
        # never silently check out with a changed price or entitlement.
        payload = [item['id'], row['product_id'], amount, points, description, bool(active),
                   promo['starts_at'] if active else 0, promo['ends_at'] if active else 0]
        digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()[:24]
        public_id = item['id'] + '__' + digest
        legacy = next((p for p in PACKAGES if p['id'] == item['id']), None)
        if (not active and legacy and row['product_id'] == legacy['id']
                and amount == legacy['yuan'] * 100 and points == legacy['points'] and not row['description'].strip()):
            public_id = legacy['id']
        offers.append({'id': public_id, 'product_id': row['product_id'], 'amount_fen': amount,
                       'points': points, 'bonus': bonus, 'bonus_text': description,
                       'promotion_active': bool(active), 'regular_amount_fen': item['amount_fen'],
                       'regular_price_text': price_text(item['amount_fen']) if active and amount < item['amount_fen'] else '',
                       'promotion_ends_at': promo['ends_at'] if active else 0})
    return offers


def resolve_offer(catalog, public_id, now=None):
    offer = next((p for p in active_offers(catalog, now) if p['id'] == public_id), None)
    if offer is None:
        raise ValueError('套餐价格或活动已更新，请返回光子中心重新进入后再购买')
    return copy.deepcopy(offer)


def public_packages(generation_cost=GENERATION_COST, catalog=None, now=None):
    return [{"id": p['id'], "yuan": p['amount_fen'] / 100, "points": p['points'], "bonus": p['bonus'],
             "price_text": price_text(p['amount_fen']), "bonus_text": p['bonus_text'],
             "amount_fen":p["amount_fen"], "promotion_active":p["promotion_active"],
             "regular_price_text":p["regular_price_text"], "regular_amount_fen":p["regular_amount_fen"],
             "promotion_ends_at":p["promotion_ends_at"],
             "generations": p['points'] // generation_cost if generation_cost > 0 else 0}
            for p in active_offers(catalog, now)]


def next_offer_change(catalog, now):
    boundaries = [stamp for item in catalog if item['enabled'] and item['promotion']['enabled']
                  for stamp in (item['promotion']['starts_at'], item['promotion']['ends_at']) if stamp > now]
    return min(boundaries) if boundaries else 0
