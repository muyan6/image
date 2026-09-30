# -*- coding: utf-8 -*-
"""模板与分组存储（backend/data/templates.json）。

设计目标：新增/修改风格不再动代码 —— 全部在 /admin 后台热调，
小程序 GET /api/templates 现读，改完即生效。

每个模板携带完整的产品定义，用户按"想要什么"选择，
而不是按抽象档位选择：

    引擎(light/fine -> 网关模型) · 提示词 · 输出分辨率 · 小鱼干价格
    · 文字排版字段(海报/明信片) · 示例图(COS/本地/外链) · 排序/开关

存储结构（单文件保证分组与模板的一致性校验简单可靠）：

    {"version": 1, "groups": [...], "templates": [...]}

写路径全部原子写(临时文件 + os.replace)，读走快照深拷贝，
损坏自动备份回种子数据 —— 与 settings_store 同一套纪律。
"""
from __future__ import annotations

import copy
import json
import logging
import os
import re
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

log = logging.getLogger("rescue.templates")

# 文字排版角色：排版引擎按角色决定字号/位置/颜色
TEXT_ROLES = ("title", "subtitle", "date", "sign", "body")
# 排版预设：见 text_overlay.py
TEXT_LAYOUTS = ("postcard_bottom", "poster_center", "stamp_corner")
ENGINES = ("light", "fine")
_GROUP_ID_RE = re.compile(r"^[a-z0-9_]{1,24}$")
_FIELD_KEY_RE = re.compile(r"^[a-z0-9_]{1,20}$")
_SIZE_RE = re.compile(r"^\d{3,4}x\d{3,4}$")


# --------------------------------------------------------------------------- #
# 种子数据：迁移原有三档风格 + 市场验证过的爆款方向
# --------------------------------------------------------------------------- #
def _seed_groups() -> List[Dict[str, Any]]:
    return [
        {"id": "restore", "name": "修复增强", "sort": 1, "enabled": True,
         "created_at": time.time()},
        {"id": "anime", "name": "动漫手办", "sort": 2, "enabled": True,
         "created_at": time.time()},
        {"id": "poster", "name": "海报日签", "sort": 3, "enabled": True,
         "created_at": time.time()},
        {"id": "postcard", "name": "明信片贺卡", "sort": 4, "enabled": True,
         "created_at": time.time()},
    ]


def _seed_templates() -> List[Dict[str, Any]]:
    """提示词即产品：每条都可后台继续调，覆盖本地引擎不认识的新风格。
    guide 是模板详情页的选图指南（建议/适合/不适合/上传提示），
    与提示词一样属于运营内容，全部后台可改。
    """
    now = time.time()

    def tpl(tid: str, group: str, name: str, subtitle: str, prompt: str,
            engine: str = "light", price: int = 40, sort: int = 1,
            output_size: int = 0, gateway_size: str = "",
            model_override: str = "", layout: str = "",
            text_fields: Optional[List[Dict[str, Any]]] = None,
            guide: Optional[Dict[str, Any]] = None,
            covers: Optional[List[str]] = None) -> Dict[str, Any]:
        return {
            "id": tid, "group_id": group, "name": name, "subtitle": subtitle,
            "cover": "", "covers": list(covers or []), "prompt": prompt, "engine": engine, "price": price,
            "output_size": output_size, "gateway_size": gateway_size,
            "model_override": model_override, "layout": layout,
            "text_fields": text_fields or [],
            "guide": _norm_guide(guide or {}),
            "sort": sort, "enabled": True, "usage_count": 0,
            "cover_v": 0, "created_at": now, "updated_at": now,
        }

    return [
        # ---- 修复增强：原有三档风格 + 精细档迁移为模板 ----
        tpl("t_clarity", "restore", "冷白通透", "去黄除暗，高光清爽，冷白皮质感",
            "对这张照片做克制、自然的优化：修复曝光与白平衡，去除噪点与灰蒙感，"
            "提升清晰度与通透感；肤色向干净冷白微调，高光清爽不溢出。"
            "色彩必须忠实于原始色调与真实氛围，不要过度饱和，不要HDR风格。"
            "严格保持画面内容、构图与人物特征不变，不添加或删除物体，不加文字与水印。",
            engine="light", sort=1,
            guide={
                "advice": "适合日常自拍、街拍与人像的通用修复。原图信息越完整，修复越自然——它做的是\"还原本来的样子\"，不是换一张脸。",
                "suitable": ["人物清晰、光线正常的日常照", "轻微欠曝、偏色或发灰的照片", "自拍、街拍、合影皆可"],
                "unsuitable": ["重度模糊或像素过低的图片", "强逆光只剩剪影的照片", "已经精修过度的图"],
                "tips": ["优先选原图导出的照片，聊天软件转发的图压缩损失大", "脸上有反光或过曝时，换一张光线柔和的"],
            }),
        tpl("t_fuji", "restore", "富士胶片", "微暖复古，温润通透，街拍人像百搭",
            "把这张照片调成富士经典胶片色调：微暖的复古色温，温润的绿色与奶油色高光，"
            "细腻的胶片颗粒，通透不油腻。保持画面内容、构图与人物特征不变，"
            "色彩过渡自然，不过度饱和，不加文字与水印。",
            engine="light", sort=2,
            guide={
                "advice": "胶片色调对光线层次最敏感，晨昏、街景与绿植场景出片最稳。",
                "suitable": ["街拍、旅行、静物", "画面里有天空、光影或植物层次", "暖调或中性光线的场景"],
                "unsuitable": ["纯白背景的证件照类图片", "夜间极暗光的手机直出图"],
                "tips": ["画面里带一点绿色植物或暖色灯光，胶片感更强", "阴天灰蒙的照片先走一次冷白通透再上胶片色更干净"],
            }),
        tpl("t_gym", "restore", "力量高反差", "强化轮廓线条，低饱和冷黑金",
            "对这张照片做力量感高反差调色：强化肌肉轮廓与边缘线条，"
            "低饱和冷调，黑金质感，突出立体感与皮肤纹理细节。"
            "保持画面内容与构图不变，避免死黑与过曝，不加文字与水印。",
            engine="light", sort=3,
            guide={
                "advice": "需要清晰的肌肉线条或轮廓结构才能体现反差质感，平光自拍效果会打折。",
                "suitable": ["健身、运动、游泳等力量场景", "侧光或顶光拍摄的轮廓分明的人像"],
                "unsuitable": ["远景大全身、主体太小", "美颜磨皮过度的照片（线条已被抹平）"],
                "tips": ["拍摄时光源在侧上方最出效果", "深色背景比浅色背景更衬托黑金质感"],
            }),
        tpl("t_master", "restore", "深度超分", "生成式细节重构，发丝纤毫毕现",
            "以专业修图师的标准对这张照片做自然的高级优化：修复曝光、白平衡、"
            "噪点与瑕疵，提升清晰度与细节层次，呈现通透、真实的质感。"
            "色彩保持克制与忠实——保留原始色调倾向与氛围，避免过度饱和、"
            "避免浓艳滤镜与HDR感。保持主体、构图与人物特征可辨认，不加文字与水印。",
            engine="fine", sort=4,
            guide={
                "advice": "万能修复档：生成式重构微观细节，对老照片和压缩图收益最大。",
                "suitable": ["老照片翻新", "聊天记录里被压缩的图", "模糊但五官可辨认的人像"],
                "unsuitable": ["原图本来就非常清晰时提升有限"],
                "tips": ["手里有原文件就直接传原文件，别传二次截图", "老照片有折痕破损也能一并修复"],
            }),
        # ---- 动漫手办与流行艺术：热门风格 ----
        tpl("t_anime_dots", "anime", "日漫错彩网点", "把真人照片重绘成复古日漫彩页肖像",
            "把真人照片重绘成复古日漫彩页肖像风格：保留原照片中人物的五官、表情、发型和姿态，"
            "用荧光专色、漫画网点、粗墨线与轻微套色错位，制作一张鲜明、复古又有冲击力的流行艺术肖像。"
            "画面具有强烈的复古波普与日漫插画质感，色彩鲜活，不加文字与水印。",
            engine="fine", sort=1,
            guide={
                "advice": "保留原照片中人物的五官、表情、发型和姿态，用荧光专色、漫画网点、粗墨线与轻微套色错位，制作一张鲜明、复古又有冲击力的流行艺术肖像。",
                "suitable": [
                    "单人正脸、侧脸或半身人像",
                    "人物五官清楚、脸部没有严重遮挡",
                    "发型轮廓明显，头发细节丰富",
                    "带有眼镜、发箍、耳饰等辨识度配件",
                    "表情自然或具有明显情绪"
                ],
                "unsuitable": [
                    "多人合照或远景全身照",
                    "口罩、手掌遮挡严重的面部",
                    "过暗、严重欠曝的照片"
                ],
                "tips": [
                    "优先选择脸部清晰、发型完整、表情有特点的单人人像",
                    "带有眼镜、耳饰或发饰等配饰能大幅丰富漫画细节"
                ]
            }),
        tpl("t_felt", "anime", "毛毡旅行档案", "手工立体羊毛毡刺绣定格质感",
            "把照片重绘成温暖的手工羊毛毡与刺绣定格动画质感：画面呈现圆润毛绒的毛毡织物纹理，"
            "边缘带有细密柔软的纤维毛刺感，色彩温润治愈，微缩定格景深。保持人物五官特征与构图不变，不加文字与水印。",
            engine="fine", sort=2,
            guide={
                "advice": "羊毛毡质感能赋予照片手作童话的温度，日常风景、街景和萌宠尤其生动。",
                "suitable": ["旅行街拍、风景与萌宠", "半身人像或大头特写", "色彩鲜明的场景"],
                "unsuitable": ["画面元素过度繁杂的群像"],
                "tips": ["鲜艳温暖的光线能更好凸显羊毛绒感"]
            }),
        tpl("t_ink_wash", "restore", "柔墨纸绘", "清透水墨宣纸晕染，东方美学留白",
            "把照片创作成东方意境的宣纸水墨淡彩画：淡雅的墨色线条勾勒轮廓，细腻的宣纸吸墨晕染纹理，"
            "虚实相生的留白意境，色彩清透脱俗。保持主体神韵与主要轮廓，不加文字与水印。",
            engine="light", sort=5,
            guide={
                "advice": "东方水墨意境重在神韵与留白，自然风光、古风汉服和植物出片最有意境。",
                "suitable": ["汉服国风、自然山水、植物与静物", "姿态舒展的人物人像"],
                "unsuitable": ["极度杂乱的现代工业场景"],
                "tips": ["画面留白较多的照片最具艺术呼吸感"]
            }),
        tpl("t_marker", "anime", "马克笔小人像", "二次元手绘插画，明快马克笔笔触",
            "把照片绘制成手绘二次元插画风格：明快的马克笔渐变笔触、清晰的深色线稿、高光点缀与卡通大眼神韵，"
            "生动可爱。保持人物发型五官特征与服饰色彩，不加文字与水印。",
            engine="fine", sort=3,
            guide={
                "advice": "手绘马克笔风格赋予人像青春明亮的动漫画风，人物肖像、自拍、二次元爱好者首选。",
                "suitable": ["单人半身、自拍与特写", "五官清晰、表情生动的人像"],
                "unsuitable": ["远景大全身、模糊无光照的自拍"],
                "tips": ["发型完整、服饰有细节的照片出片最惊艳"]
            }),
        tpl("t_childhood", "restore", "童年高清", "胶片颗粒与暖阳质感，童年回忆重现",
            "对照片进行童年老照片风格的高清质感重塑：细腻温暖的柯达暖阳胶片色调，"
            "真实自然的微观皮肤细节与发丝超分重构，抚平模糊噪点，留住原初温度。严格保持面部特征与真实神态，不加文字与水印。",
            engine="fine", sort=6,
            guide={
                "advice": "专注于老旧、泛黄、模糊儿童照与怀旧照的真实高保真重现，保留岁月温情。",
                "suitable": ["童年旧照、家庭胶卷老相片", "翻拍老相册、轻度受损模糊旧照"],
                "unsuitable": ["面部已经大面积撕裂缺失的照片"],
                "tips": ["平整翻拍相册，避免强反光"]
            }),
        tpl("t_ghibli", "anime", "吉卜力童话", "手绘水彩暖阳，宫崎骏式自然场景",
            "把这张照片转绘成吉卜力工作室手绘动画风格：柔和的水彩天空、"
            "明快的暖色阳光、细腻的手绘笔触；人物保留原有姿态与可辨识的五官特征，"
            "背景演绎成宫崎骏式的自然场景（层叠云朵、草原或森林）。"
            "画面干净治愈，色彩明亮通透，不加文字与水印。",
            engine="fine", sort=4,
            guide={
                "advice": "保留原照片中人物的五官、表情、发型和姿态，用水彩手绘质感重绘；人物的可辨识度取决于原图清晰度。",
                "suitable": ["单人正脸、侧脸或半身人像", "人物五官清楚、脸部没有严重遮挡", "发型轮廓明显，头发细节丰富"],
                "unsuitable": ["多人合照或人群照片", "人物距离太远的全身照", "脸部严重模糊、过曝或欠曝", "口罩、手掌、头发大面积遮住五官"],
                "tips": ["优先选择脸部清晰、发型完整、表情有特点的单人人像", "正脸、侧脸和夸张表情都可以，但要确保眼睛、鼻子、嘴部和下颌轮廓能够看清"],
            }),
        tpl("t_clay", "anime", "粘土小屋", "定格动画黏土质感，圆润可爱",
            "把这张照片变成黏土定格动画质感：画面主体变成圆润可爱的黏土材质，"
            "保留五官特征与姿态，表面有细微的手工指纹与捏塑痕迹，"
            "背景是微缩黏土布景，柔和的影棚灯光，轻微浅景深。不加文字与水印。",
            engine="fine", sort=5,
            guide={
                "advice": "黏土质感会放大面部特征，走可爱系；大头照和宠物照效果最好。",
                "suitable": ["单人大头照、表情生动", "宠物猫狗正面照", "头肩比例大的构图"],
                "unsuitable": ["全身远景（脸部细节不够）", "画面元素过多、背景杂乱"],
                "tips": ["表情越夸张越有定格动画的喜感", "正面平视角度比俯拍仰拍更稳"],
            }),
        tpl("t_figure", "anime", "3D手办盲盒", "收藏级PVC手办，立在底座上",
            "把照片里的主体变成收藏级3D手办盲盒：Q版比例、光滑PVC材质、"
            "精细涂装与分色，站在圆形展示底座上，旁边是半透明亚克力包装盒，"
            "影棚打光，浅景深。保持人物五官与发型特征可辨识。不加文字与水印。",
            engine="fine", sort=6,
            guide={
                "advice": "手办化需要完整的人物轮廓来生成立体感与底座透视，大头特写撑不起构图。",
                "suitable": ["半身或全身单人，姿势明确", "穿着有辨识度（制服、特色服装加分）"],
                "unsuitable": ["只有大头特写", "多人重叠遮挡"],
                "tips": ["背景干净的照片，底座和亚克力包装盒会更真实", "手部姿势自然的手办完成度更高"],
            }),
        # ---- 海报日签：AI 底图 + 代码排版中文 ----
        tpl("t_poster", "poster", "复古电影海报", "戏剧光影底图 + 中文标题排版",
            "把这张照片创作成复古电影海报的底图：强烈的戏剧化光影，"
            "电影感调色，适当的颗粒质感；主体置于画面下方三分之二区域，"
            "画面下部偏暗、干净，留出排版空间。保持主体可辨识。"
            "画面本身绝对不要包含任何文字、字母或标志。",
            engine="fine", sort=1, layout="poster_center",
            text_fields=[
                {"key": "title", "label": "主标题", "role": "title",
                 "default": "盛夏光年", "max_len": 10},
                {"key": "subtitle", "label": "副标题", "role": "subtitle",
                 "default": "A SUMMER TALE", "max_len": 30},
            ],
            guide={
                "advice": "底图走戏剧化光影，标题文字由排版引擎叠加，保证汉字绝对正确。主体在中下部、上部有留白的照片最配这套排版。",
                "suitable": ["有明确主体的横竖构图皆可", "光影对比强、氛围感强的照片"],
                "unsuitable": ["平淡顺光的风景照", "主体太小、画面太碎"],
                "tips": ["先生成再看效果，主标题建议 2~6 个字", "想换文案不用重新生成底图——但本版本会整体重排"],
            }),
        # ---- 明信片贺卡：印刷级 2K 输出 ----
        tpl("t_postcard", "postcard", "旅行明信片", "版画风底图 + 落款排版，可冲印",
            "把这张照片转绘成复古旅行明信片的画面：浓郁而通透的版画风配色，"
            "细腻的印刷纹理，轻微暗角，构图干净适合排版。"
            "保持主体与场景可辨识。画面本身绝对不要包含任何文字或邮戳。",
            engine="fine", price=40, sort=1, output_size=2048,
            layout="postcard_bottom",
            text_fields=[
                {"key": "title", "label": "问候语", "role": "title",
                 "default": "来自远方的问候", "max_len": 14},
                {"key": "place", "label": "地点", "role": "subtitle",
                 "default": "WANDERLUST POST", "max_len": 30},
                {"key": "date", "label": "日期", "role": "date",
                 "default": "{today}", "max_len": 24},
            ],
            guide={
                "advice": "输出为 2048px 长边的印刷级 2K 图，可直接冲印；问候语、地点、日期由排版引擎叠加，文字保证正确。",
                "suitable": ["旅行、风景、建筑", "色彩浓郁、构图干净的照片"],
                "unsuitable": ["低分辨率截图", "画面过暗或焦点不实"],
                "tips": ["地点用英文或短拼音更有邮票感", "日期默认今天，可以在提交页改"],
            }),
    ]


def _norm_guide(raw: Any) -> Dict[str, Any]:
    """归一化选图指南：advice 一段话 + suitable/unsuitable/tips 三组清单。

    清单支持列表或"一行一条"的字符串（后台 textarea 直接粘贴），
    全部裁剪到安全长度 —— 指南是运营内容，坏数据不该让保存失败。
    """
    if not isinstance(raw, dict):
        return {"advice": "", "suitable": [], "unsuitable": [], "tips": []}

    def lines(value: Any, limit: int = 8, width: int = 60) -> List[str]:
        if isinstance(value, str):
            items = [s.strip() for s in value.splitlines() if s.strip()]
        elif isinstance(value, list):
            items = [str(s).strip() for s in value if str(s).strip()]
        else:
            items = []
        return [i[:width] for i in items[:limit]]

    return {
        "advice": str(raw.get("advice", "") or "").strip()[:500],
        "suitable": lines(raw.get("suitable")),
        "unsuitable": lines(raw.get("unsuitable")),
        "tips": lines(raw.get("tips")),
    }


def _validate(doc: Dict[str, Any]) -> None:
    """整档校验，不合法抛 ValueError（调用方放弃写入）。"""
    groups = doc.get("groups")
    templates = doc.get("templates")
    if not isinstance(groups, list) or not isinstance(templates, list):
        raise ValueError("groups/templates 必须是列表")

    group_ids = set()
    for g in groups:
        if not isinstance(g, dict) or not _GROUP_ID_RE.match(str(g.get("id", ""))):
            raise ValueError("分组 id 只允许小写字母/数字/下划线（1~24 位）: %r"
                             % (g or {}).get("id"))
        if g["id"] in group_ids:
            raise ValueError("分组 id 重复: %r" % g["id"])
        if not str(g.get("name", "")).strip() or len(str(g["name"])) > 20:
            raise ValueError("分组名必填且不超过 20 字: %r" % g["id"])
        if not isinstance(g.get("sort", 0), int):
            raise ValueError("分组 sort 必须是整数")
        if not isinstance(g.get("enabled", True), bool):
            raise ValueError("分组 enabled 必须是布尔值")
        group_ids.add(g["id"])

    tpl_ids = set()
    for t in templates:
        tid = str((t or {}).get("id", ""))
        if not re.fullmatch(r"[0-9a-z_]{4,32}", tid):
            raise ValueError("模板 id 非法: %r" % tid)
        if tid in tpl_ids:
            raise ValueError("模板 id 重复: %r" % tid)
        tpl_ids.add(tid)
        if t.get("group_id") not in group_ids:
            raise ValueError("模板 %s 指向不存在的分组 %r" % (tid, t.get("group_id")))
        if not str(t.get("name", "")).strip() or len(str(t["name"])) > 20:
            raise ValueError("模板 %s 名称必填且不超过 20 字" % tid)
        if len(str(t.get("subtitle", ""))) > 60:
            raise ValueError("模板 %s 副标题不超过 60 字" % tid)
        prompt = str(t.get("prompt", "")).strip()
        if not prompt or len(prompt) > 4000:
            raise ValueError("模板 %s 提示词必填且不超过 4000 字" % tid)
        if t.get("engine") not in ENGINES:
            raise ValueError("模板 %s engine 只能是 light/fine" % tid)
        for field, top in (("price", 9999), ("output_size", 8192), ("sort", 9999)):
            value = t.get(field, 0)
            if not isinstance(value, int) or not 0 <= value <= top:
                raise ValueError("模板 %s 的 %s 必须是 0~%d 的整数" % (tid, field, top))
        gs = str(t.get("gateway_size", "") or "").strip()
        if gs and not _SIZE_RE.match(gs):
            raise ValueError("模板 %s gateway_size 格式应为 宽x高，如 2048x2048" % tid)
        if len(str(t.get("model_override", ""))) > 80:
            raise ValueError("模板 %s model_override 不超过 80 字" % tid)
        covers = t.get("covers")
        if covers is not None and (not isinstance(covers, list) or len(covers) > 3):
            raise ValueError("模板 %s 的 covers 必须是不超过 3 项的列表" % tid)
        layout = str(t.get("layout", "") or "")
        if layout and layout not in TEXT_LAYOUTS:
            raise ValueError("模板 %s layout 只能是 %s" % (tid, "/".join(TEXT_LAYOUTS)))
        fields = t.get("text_fields", [])
        if not isinstance(fields, list) or len(fields) > 6:
            raise ValueError("模板 %s 文字字段 0~6 个" % tid)
        keys = set()
        for f in fields:
            if not isinstance(f, dict) or not _FIELD_KEY_RE.match(str(f.get("key", ""))):
                raise ValueError("模板 %s 文字字段 key 只允许小写字母/数字/下划线" % tid)
            if f["key"] in keys:
                raise ValueError("模板 %s 文字字段 key 重复: %r" % (tid, f["key"]))
            keys.add(f["key"])
            if f.get("role") not in TEXT_ROLES:
                raise ValueError("模板 %s 文字字段 role 只能是 %s"
                                 % (tid, "/".join(TEXT_ROLES)))
            if not str(f.get("label", "")).strip() or len(str(f["label"])) > 12:
                raise ValueError("模板 %s 文字字段 label 必填且不超过 12 字" % tid)
            if len(str(f.get("default", ""))) > 100:
                raise ValueError("模板 %s 文字字段默认值不超过 100 字" % tid)
            ml = f.get("max_len", 30)
            if not isinstance(ml, int) or not 1 <= ml <= 200:
                raise ValueError("模板 %s 文字字段 max_len 是 1~200 的整数" % tid)
        # 填了文字字段就必须有排版预设
        if fields and not layout:
            raise ValueError("模板 %s 配置了文字字段，必须选择排版预设" % tid)


class TemplateStore:
    """线程安全的模板与分组存储。"""

    def __init__(self, data_dir: str) -> None:
        self._path = os.path.join(data_dir, "templates.json")
        self._lock = threading.RLock()
        os.makedirs(data_dir, exist_ok=True)
        self._groups = _seed_groups()
        self._templates = _seed_templates()
        self._load_or_init()
        self.ensure_placeholder_covers()

    def ensure_placeholder_covers(self) -> None:
        """检查所有模板，若未配置封面或本地封面文件缺失，自动生成专属占位封面。"""
        out_dir = covers_dir()
        migrated = False
        with self._lock:
            for t in self._templates:
                cover = str(t.get("cover") or "").strip()
                filename = "%s_v1.jpg" % t["id"]
                local_path = os.path.join(out_dir, filename)
                need_make = False
                if not cover:
                    need_make = True
                elif cover.startswith("local:") and not os.path.isfile(local_path):
                    need_make = True

                if need_make:
                    try:
                        data = generate_placeholder_cover(
                            t.get("name", ""), t.get("subtitle", ""), t.get("group_id", "")
                        )
                        if data:
                            with open(local_path, "wb") as fh:
                                fh.write(data)
                            t["cover"] = "local:%s" % filename
                            t["cover_v"] = max(int(t.get("cover_v", 0)), 1)
                            migrated = True
                    except Exception as exc:
                        log.warning("[%s] 自动生成占位封面跳过: %s", t.get("id"), exc)
            if migrated:
                self._save_locked()
                log.info("已自动为模板补充生成占位封面并更新数据")

    # ------------------------------------------------------------------ #
    # 持久化
    # ------------------------------------------------------------------ #
    def _load_or_init(self) -> None:
        if not os.path.exists(self._path):
            self._save_locked()
            log.info("模板初始化（种子数据）: %s", self._path)
            return
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if not isinstance(loaded, dict):
                raise ValueError("根节点不是对象")
            groups = loaded.get("groups")
            templates = loaded.get("templates")
            if not isinstance(groups, list) or not isinstance(templates, list):
                raise ValueError("groups/templates 缺失")
            candidate = {"groups": groups, "templates": templates}
            _validate(candidate)  # 坏数据直接回种子，不带病运行
            # Persisted templates are authoritative: deletion is not a migration.
            # Future seed additions must have an explicit, one-time versioned migration.
            migrated = loaded.get("version", 1) < 2
            if loaded.get("version", 1) < 3:
                # 新计费规则：现有全部模板的价格一次性统一为 40 光子。
                for t in templates:
                    t["price"] = 40
                migrated = True

            # 旧版本数据没有 guide 字段：读入时统一补齐并归一化；
            # 种子模板指南为空的回填种子内容（一次性迁移），自建模板不动
            seed_guides = {t["id"]: t["guide"] for t in _seed_templates()}
            for t in templates:
                g = _norm_guide(t.get("guide"))
                if not any([g["advice"], g["suitable"], g["unsuitable"], g["tips"]]) \
                        and t["id"] in seed_guides:
                    g = seed_guides[t["id"]]
                    migrated = True
                t["guide"] = g
            self._groups = groups
            self._templates = templates
            if migrated:
                self._save_locked()
                log.info("模板数据迁移/同步完成（已同步种子模板与选图指南）")
            log.info("模板已加载: %s（%d 组 / %d 模板）",
                     self._path, len(groups), len(templates))
        except (OSError, ValueError) as exc:
            stamp = time.strftime("%Y%m%d_%H%M%S")
            backup = "%s.corrupt_%s" % (self._path, stamp)
            try:
                os.replace(self._path, backup)
                log.error("模板文件损坏(%s)，已备份到 %s 并回退种子数据", exc, backup)
            except OSError:
                log.error("模板文件损坏(%s)，备份失败，回退种子数据", exc)
            self._groups = _seed_groups()
            self._templates = _seed_templates()
            self._save_locked()

    def _save_locked(self) -> None:
        tmp = self._path + ".tmp"
        doc = {"version": 3, "groups": self._groups, "templates": self._templates}
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, self._path)

    # ------------------------------------------------------------------ #
    # 分组
    # ------------------------------------------------------------------ #
    def list_groups(self, enabled_only: bool = False) -> List[Dict[str, Any]]:
        with self._lock:
            items = [copy.deepcopy(g) for g in self._groups
                     if (g.get("enabled") or not enabled_only)]
        items.sort(key=lambda g: (g.get("sort", 0), g.get("id", "")))
        return items

    def create_group(self, name: str, sort: int = 99,
                     enabled: bool = True) -> Dict[str, Any]:
        name = (name or "").strip()
        if not name or len(name) > 20:
            raise ValueError("分组名必填且不超过 20 字")
        # id 从名字派生（中文转拼音不可行，用 g + 随机尾巴，后台可改）
        with self._lock:
            gid = "g%s" % uuid.uuid4().hex[:6]
            while any(g["id"] == gid for g in self._groups):
                gid = "g%s" % uuid.uuid4().hex[:6]
            group = {"id": gid, "name": name, "sort": int(sort),
                     "enabled": bool(enabled), "created_at": time.time()}
            self._groups.append(group)
            self._save_locked()
            return copy.deepcopy(group)

    def update_group(self, group_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            for g in self._groups:
                if g["id"] == group_id:
                    if "name" in patch:
                        name = str(patch["name"] or "").strip()
                        if not name or len(name) > 20:
                            raise ValueError("分组名必填且不超过 20 字")
                        g["name"] = name
                    if "sort" in patch:
                        g["sort"] = int(patch["sort"])
                    if "enabled" in patch:
                        g["enabled"] = bool(patch["enabled"])
                    self._save_locked()
                    return copy.deepcopy(g)
        raise KeyError("分组不存在")

    def delete_group(self, group_id: str) -> None:
        with self._lock:
            if not any(g["id"] == group_id for g in self._groups):
                raise KeyError("分组不存在")
            used = any(t["group_id"] == group_id for t in self._templates)
            if used:
                raise ValueError("分组下还有模板，请先移走或删除它们")
            self._groups = [g for g in self._groups if g["id"] != group_id]
            self._save_locked()

    # ------------------------------------------------------------------ #
    # 模板
    # ------------------------------------------------------------------ #
    def list_templates(self, enabled_only: bool = False,
                       group_id: str = "") -> List[Dict[str, Any]]:
        with self._lock:
            items = [copy.deepcopy(t) for t in self._templates
                     if (t.get("enabled") or not enabled_only)]
        if group_id:
            items = [t for t in items if t.get("group_id") == group_id]
        items.sort(key=lambda t: (t.get("sort", 0), t.get("id", "")))
        return items

    def reprice_by_engine(self, prices: Dict[str, int]) -> int:
        """后台改档位价时，同步现有模板，避免模板旧价覆盖新的全站定价。"""
        amounts = {key: int(prices[key]) for key in ENGINES}
        if any(value < 0 or value > 9999 for value in amounts.values()):
            raise ValueError("模板价格必须是 0～9999 光子")
        with self._lock:
            for template in self._templates:
                template["price"] = amounts[template.get("engine", "light")]
                template["updated_at"] = time.time()
            self._save_locked()
            return len(self._templates)

    def get_template(self, tpl_id: str,
                     enabled_only: bool = False) -> Optional[Dict[str, Any]]:
        with self._lock:
            for t in self._templates:
                if t["id"] == tpl_id:
                    if enabled_only and not t.get("enabled"):
                        return None
                    return copy.deepcopy(t)
        return None

    def create_template(self, data: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            tid = str(data.get("id") or "").strip() or "t%s" % uuid.uuid4().hex[:8]
            if any(t["id"] == tid for t in self._templates):
                raise ValueError("模板 id 已存在: %r" % tid)
            item = {
                "id": tid,
                "group_id": str(data.get("group_id") or ""),
                "name": str(data.get("name") or ""),
                "subtitle": str(data.get("subtitle") or ""),
                "cover": str(data.get("cover") or ""),
                "prompt": str(data.get("prompt") or ""),
                "engine": str(data.get("engine") or "light"),
                "price": _as_int(data.get("price"), 0),
                "output_size": _as_int(data.get("output_size"), 0),
                "gateway_size": str(data.get("gateway_size") or ""),
                "model_override": str(data.get("model_override") or ""),
                "layout": str(data.get("layout") or ""),
                "text_fields": _norm_text_fields(data.get("text_fields")),
                "guide": _norm_guide(data.get("guide")),
                "sort": _as_int(data.get("sort"), 99),
                "enabled": bool(data.get("enabled", True)),
                "usage_count": 0,
                "cover_v": 0,
                "created_at": time.time(),
                "updated_at": time.time(),
            }
            # 先校验（含新模板的整体快照），通过才落内存 —— 校验失败不能污染现有数据
            _validate({"groups": self._groups,
                       "templates": self._templates + [item]})
            self._templates.append(item)
            self._save_locked()
            return copy.deepcopy(item)

    def update_template(self, tpl_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            for idx, t in enumerate(self._templates):
                if t["id"] != tpl_id:
                    continue
                candidate = copy.deepcopy(t)
                # id 不允许改；其余逐字段吸收
                mapping = {
                    "group_id": str, "name": str, "subtitle": str, "cover": str,
                    "prompt": str, "engine": str, "gateway_size": str,
                    "model_override": str, "layout": str,
                }
                for key, caster in mapping.items():
                    if key in patch:
                        candidate[key] = caster(patch[key] or "")
                for key in ("price", "output_size", "sort"):
                    if key in patch:
                        candidate[key] = _as_int(patch[key], candidate.get(key, 0))
                if "enabled" in patch:
                    candidate["enabled"] = bool(patch["enabled"])
                if "covers" in patch:
                    raw_c = patch["covers"]
                    if isinstance(raw_c, list):
                        cleaned_c = [str(x).strip() for x in raw_c if str(x).strip()][:3]
                        candidate["covers"] = cleaned_c
                        candidate["cover"] = cleaned_c[0] if cleaned_c else ""
                if "text_fields" in patch:
                    candidate["text_fields"] = _norm_text_fields(patch["text_fields"])
                if "guide" in patch:
                    candidate["guide"] = _norm_guide(patch["guide"])
                candidate["updated_at"] = time.time()
                # 先校验改完的整表快照，通过才替换内存 —— 失败时原数据原样保留
                merged = [candidate if i == idx else copy.deepcopy(x)
                          for i, x in enumerate(self._templates)]
                _validate({"groups": self._groups, "templates": merged})
                self._templates = merged
                self._save_locked()
                return copy.deepcopy(candidate)
        raise KeyError("模板不存在")

    def delete_template(self, tpl_id: str) -> None:
        with self._lock:
            before = len(self._templates)
            self._templates = [t for t in self._templates if t["id"] != tpl_id]
            if len(self._templates) == before:
                raise KeyError("模板不存在")
            self._save_locked()

    def inc_usage(self, tpl_id: str) -> None:
        """成功交付后累计一次；历史累计基数保留，失败和排队不再增加。"""
        with self._lock:
            for t in self._templates:
                if t["id"] == tpl_id:
                    t["usage_count"] = int(t.get("usage_count", 0)) + 1
                    self._save_locked()
                    return

    def set_cover(self, tpl_id: str, cover: str) -> Dict[str, Any]:
        """兼容老接口：默认写第 0 槽位。"""
        return self.set_cover_slot(tpl_id, 0, cover)

    def set_cover_slot(self, tpl_id: str, slot: int, cover: str) -> Dict[str, Any]:
        """设置模板的第 slot 张示例图（slot 为 0, 1, 2）。"""
        slot = max(0, min(int(slot), 2))
        with self._lock:
            for t in self._templates:
                if t["id"] == tpl_id:
                    covers = list(t.get("covers") or [])
                    if not covers and t.get("cover"):
                        covers = [t["cover"]]
                    while len(covers) <= slot:
                        covers.append("")
                    covers[slot] = cover
                    while covers and not covers[-1]:
                        covers.pop()
                    t["covers"] = covers[:3]
                    t["cover"] = covers[0] if covers else ""
                    t["cover_v"] = int(t.get("cover_v", 0) or 0) + 1
                    t["updated_at"] = time.time()
                    self._save_locked()
                    return copy.deepcopy(t)
        raise KeyError("模板不存在")

    def delete_cover_slot(self, tpl_id: str, slot: int) -> Dict[str, Any]:
        """删除模板的第 slot 张示例图（slot 为 0, 1, 2）。"""
        slot = max(0, min(int(slot), 2))
        with self._lock:
            for t in self._templates:
                if t["id"] == tpl_id:
                    covers = list(t.get("covers") or [])
                    if not covers and t.get("cover"):
                        covers = [t["cover"]]
                    if slot < len(covers):
                        covers.pop(slot)
                    t["covers"] = covers
                    t["cover"] = covers[0] if covers else ""
                    t["cover_v"] = int(t.get("cover_v", 0) or 0) + 1
                    t["updated_at"] = time.time()
                    self._save_locked()
                    return copy.deepcopy(t)
        raise KeyError("模板不存在")

    # ------------------------------------------------------------------ #
    # 公开接口的精简投影：提示词不下发（那是调教出来的东西）
    def public_templates(self, settings) -> List[Dict[str, Any]]:
        """给小程序的模板列表：解析封面与多张示例图 URL，附带分组名。"""
        group_names = {g["id"]: g["name"] for g in self.list_groups(enabled_only=True)}
        out = []
        for t in self.list_templates(enabled_only=True):
            resolved_list = resolve_covers(t, settings)
            main_cover = resolved_list[0] if resolved_list else resolve_cover(t, settings)
            out.append({
                "id": t["id"],
                "group_id": t["group_id"],
                "group_name": group_names.get(t["group_id"], ""),
                "name": t["name"],
                "subtitle": t.get("subtitle", ""),
                "cover": main_cover,
                "covers": resolved_list,
                "engine": t["engine"],
                "price": int(t.get("price", 0)),
                "usage_count": max(0,int(t.get('usage_count',0))),
                "quality_options": ['light','fine'],
                "tier_prices": settings.prices(),
                "layout": t.get("layout", ""),
                "text_fields": t.get("text_fields", []),
                "guide": _norm_guide(t.get("guide")),
            })
        return out


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _norm_text_fields(raw: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out = []
    for f in raw:
        if not isinstance(f, dict):
            continue
        out.append({
            "key": str(f.get("key") or "").strip(),
            "label": str(f.get("label") or "").strip(),
            "role": str(f.get("role") or "").strip(),
            "default": str(f.get("default") or ""),
            "max_len": _as_int(f.get("max_len"), 30),
        })
    return out


# 分组主题色（上 -> 下渐变）
THEMES = {
    "restore": [(214, 226, 235), (168, 196, 214)],
    "anime": [(255, 224, 214), (244, 172, 154)],
    "poster": [(38, 44, 58), (74, 85, 104)],
    "postcard": [(240, 230, 210), (206, 186, 156)],
}
FALLBACK_THEME = [(226, 232, 240), (178, 190, 205)]

FONT_CANDIDATES = [
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/wqy-microhei/wqy-microhei.ttc",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/PingFang.ttc",
]


def generate_placeholder_cover(name: str, subtitle: str, group: str) -> bytes:
    """生成 720x900（3:4）的高清精美占位封面图。"""
    import io
    import random
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return b""

    W, H = 720, 900
    top, bottom = THEMES.get(group, FALLBACK_THEME)
    img = Image.new("RGB", (W, H))
    for y in range(H):
        t = y / H
        color = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        for x in range(0, W, 4):
            for dx in range(4):
                img.putpixel((min(x + dx, W - 1), y), color)

    draw = ImageDraw.Draw(img, "RGBA")
    rng = random.Random(name)
    for _ in range(24):
        r = rng.randint(8, 90)
        x, y = rng.randint(-40, W + 40), rng.randint(-40, H + 40)
        alpha = rng.randint(10, 34)
        draw.ellipse([x - r, y - r, x + r, y + r], fill=(255, 255, 255, alpha))
    for _ in range(700):
        x, y = rng.randint(0, W - 1), rng.randint(0, H - 1)
        draw.point((x, y), fill=(255, 255, 255, rng.randint(8, 26)))

    def _font(size: int):
        for cand in FONT_CANDIDATES:
            if os.path.isfile(cand):
                try:
                    return ImageFont.truetype(cand, size)
                except OSError:
                    continue
        return ImageFont.load_default()

    big = _font(int(H * 0.42))
    ch = name[0] if name else "图"
    try:
        bw = draw.textlength(ch, font=big)
    except AttributeError:
        bw = big.getsize(ch)[0]
    draw.text(((W - bw) / 2, H * 0.18), ch, font=big, fill=(255, 255, 255, 92))

    band_top = int(H * 0.76)
    draw.rectangle([0, band_top, W, H], fill=(20, 24, 33, 200))
    pad = int(W * 0.06)
    name_font = _font(int(W * 0.072))
    sub_font = _font(int(W * 0.032))
    draw.text((pad, band_top + int(H * 0.035)), name, font=name_font,
              fill=(250, 250, 248, 255))
    if subtitle:
        draw.text((pad, band_top + int(H * 0.115)), subtitle[:24],
                  font=sub_font, fill=(203, 213, 225, 235))
    draw.text((pad, H - int(H * 0.035)), "PLACEHOLDER · 风格图鉴",
              font=_font(int(W * 0.026)), fill=(148, 163, 184, 220))
    draw.rectangle([6, 6, W - 7, H - 7], outline=(255, 255, 255, 150), width=3)

    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88)
    return buf.getvalue()


def resolve_cover_ref(cover: str, version: int, settings, tid: str = "") -> str:
    """把单个 cover 引用解析成可访问 URL。"""
    cover = str(cover or "").strip()
    if not cover:
        if tid:
            v1_name = "%s_v1.jpg" % tid
            if os.path.isfile(os.path.join(_covers_dir(), v1_name)):
                return "/api/covers/%s?v=1" % v1_name
        return ""
    if cover.startswith(("http://", "https://")):
        return cover
    if cover.startswith("cos:"):
        key = cover[4:].lstrip("/")
        try:
            from cos_store import presign as cos_presign
            return cos_presign(settings, "get", key, ttl_seconds=7200)
        except Exception:  # noqa: BLE001 —— COS 未配置时退本地副本
            filename = os.path.basename(key)
            if os.path.isfile(os.path.join(_covers_dir(), filename)):
                return "/api/covers/%s" % filename
            return ""
    if cover.startswith("local:"):
        return "/api/covers/%s?v=%d" % (os.path.basename(cover[6:]), version)
    return cover


def resolve_cover(t: Dict[str, Any], settings) -> str:
    """解析模板主封面地址。"""
    raw_covers = t.get("covers")
    version = int(t.get("cover_v", 0) or 0)
    tid = t.get("id", "")
    if isinstance(raw_covers, list) and raw_covers:
        first = raw_covers[0]
        if first:
            return resolve_cover_ref(first, version, settings, tid)
    return resolve_cover_ref(t.get("cover", ""), version, settings, tid)


def resolve_covers(t: Dict[str, Any], settings) -> List[str]:
    """解析模板的示例图列表（至多 3 张有效地址）。"""
    raw_covers = t.get("covers")
    version = int(t.get("cover_v", 0) or 0)
    tid = t.get("id", "")
    if isinstance(raw_covers, list) and raw_covers:
        res = [resolve_cover_ref(c, version, settings, tid) for c in raw_covers if str(c or "").strip()]
        if res:
            return res
    c = resolve_cover(t, settings)
    return [c] if c else []


_COVERS_DIR_CACHE: Dict[str, str] = {}


def _covers_dir() -> str:
    if "dir" not in _COVERS_DIR_CACHE:
        _COVERS_DIR_CACHE["dir"] = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "data", "covers")
    return _COVERS_DIR_CACHE["dir"]


def covers_dir() -> str:
    """封面本地落盘目录（也是上传的降级存储）。"""
    path = _covers_dir()
    os.makedirs(path, exist_ok=True)
    return path
