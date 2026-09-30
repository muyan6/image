"""光子充值商品目录；微信虚拟支付商品需在公众平台按同一价格上架。"""

POINTS_PER_YUAN = 100
GENERATION_COST = 40

PACKAGES = (
    {"id": "points_600", "yuan": 6, "points": 600, "bonus": 0},
    {"id": "points_3300", "yuan": 30, "points": 3300, "bonus": 300},
    {"id": "points_8160", "yuan": 68, "points": 8160, "bonus": 1360},
    {"id": "points_16640", "yuan": 128, "points": 16640, "bonus": 3840},
)


def public_packages():
    return [{**item, "price_text": "¥%d" % item["yuan"],
             "generations": item["points"] // GENERATION_COST,
             "bonus_text": "多送 %d 光子" % item["bonus"] if item["bonus"] else "标准 1 元 = 100 光子"}
            for item in PACKAGES]
