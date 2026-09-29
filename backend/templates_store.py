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
    """提示词即产品：每条都可后台继续调，覆盖本地引擎不认识的新风格。"""
    now = time.time()

    def tpl(tid: str, group: str, name: str, subtitle: str, prompt: str,
            engine: str = "light", price: int = 0, sort: int = 1,
            output_size: int = 0, gateway_size: str = "",
            model_override: str = "", layout: str = "",
            text_fields: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        return {
            "id": tid, "group_id": group, "name": name, "subtitle": subtitle,
            "cover": "", "prompt": prompt, "engine": engine, "price": price,
            "output_size": output_size, "gateway_size": gateway_size,
            "model_override": model_override, "layout": layout,
            "text_fields": text_fields or [],
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
            engine="light", sort=1),
        tpl("t_fuji", "restore", "富士胶片", "微暖复古，温润通透，街拍人像百搭",
            "把这张照片调成富士经典胶片色调：微暖的复古色温，温润的绿色与奶油色高光，"
            "细腻的胶片颗粒，通透不油腻。保持画面内容、构图与人物特征不变，"
            "色彩过渡自然，不过度饱和，不加文字与水印。",
            engine="light", sort=2),
        tpl("t_gym", "restore", "力量高反差", "强化轮廓线条，低饱和冷黑金",
            "对这张照片做力量感高反差调色：强化肌肉轮廓与边缘线条，"
            "低饱和冷调，黑金质感，突出立体感与皮肤纹理细节。"
            "保持画面内容与构图不变，避免死黑与过曝，不加文字与水印。",
            engine="light", sort=3),
        tpl("t_master", "restore", "深度超分", "生成式细节重构，发丝纤毫毕现",
            "以专业修图师的标准对这张照片做自然的高级优化：修复曝光、白平衡、"
            "噪点与瑕疵，提升清晰度与细节层次，呈现通透、真实的质感。"
            "色彩保持克制与忠实——保留原始色调倾向与氛围，避免过度饱和、"
            "避免浓艳滤镜与HDR感。保持主体、构图与人物特征可辨认，不加文字与水印。",
            engine="fine", sort=4),
        # ---- 动漫手办：近年验证过的三大爆款方向 ----
        tpl("t_ghibli", "anime", "吉卜力童话", "手绘水彩暖阳，宫崎骏式自然场景",
            "把这张照片转绘成吉卜力工作室手绘动画风格：柔和的水彩天空、"
            "明快的暖色阳光、细腻的手绘笔触；人物保留原有姿态与可辨识的五官特征，"
            "背景演绎成宫崎骏式的自然场景（层叠云朵、草原或森林）。"
            "画面干净治愈，色彩明亮通透，不加文字与水印。",
            engine="fine", sort=1),
        tpl("t_clay", "anime", "粘土小屋", "定格动画黏土质感，圆润可爱",
            "把这张照片变成黏土定格动画质感：画面主体变成圆润可爱的黏土材质，"
            "保留五官特征与姿态，表面有细微的手工指纹与捏塑痕迹，"
            "背景是微缩黏土布景，柔和的影棚灯光，轻微浅景深。不加文字与水印。",
            engine="fine", sort=2),
        tpl("t_figure", "anime", "3D手办盲盒", "收藏级PVC手办，立在底座上",
            "把照片里的主体变成收藏级3D手办盲盒：Q版比例、光滑PVC材质、"
            "精细涂装与分色，站在圆形展示底座上，旁边是半透明亚克力包装盒，"
            "影棚打光，浅景深。保持人物五官与发型特征可辨识。不加文字与水印。",
            engine="fine", sort=3),
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
            ]),
        # ---- 明信片贺卡：印刷级 2K 输出 ----
        tpl("t_postcard", "postcard", "旅行明信片", "版画风底图 + 落款排版，可冲印",
            "把这张照片转绘成复古旅行明信片的画面：浓郁而通透的版画风配色，"
            "细腻的印刷纹理，轻微暗角，构图干净适合排版。"
            "保持主体与场景可辨识。画面本身绝对不要包含任何文字或邮戳。",
            engine="fine", price=4, sort=1, output_size=2048,
            layout="postcard_bottom",
            text_fields=[
                {"key": "title", "label": "问候语", "role": "title",
                 "default": "来自远方的问候", "max_len": 14},
                {"key": "place", "label": "地点", "role": "subtitle",
                 "default": "WANDERLUST POST", "max_len": 30},
                {"key": "date", "label": "日期", "role": "date",
                 "default": "{today}", "max_len": 24},
            ]),
    ]


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
            self._groups = groups
            self._templates = templates
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
        doc = {"version": 1, "groups": self._groups, "templates": self._templates}
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
                "sort": _as_int(data.get("sort"), 99),
                "enabled": bool(data.get("enabled", True)),
                "usage_count": 0,
                "cover_v": 0,
                "created_at": time.time(),
                "updated_at": time.time(),
            }
            self._templates.append(item)
            self._validate_snapshot()
            self._save_locked()
            return copy.deepcopy(item)

    def update_template(self, tpl_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            for t in self._templates:
                if t["id"] != tpl_id:
                    continue
                # id 不允许改；其余逐字段吸收
                mapping = {
                    "group_id": str, "name": str, "subtitle": str, "cover": str,
                    "prompt": str, "engine": str, "gateway_size": str,
                    "model_override": str, "layout": str,
                }
                for key, caster in mapping.items():
                    if key in patch:
                        t[key] = caster(patch[key] or "")
                for key in ("price", "output_size", "sort"):
                    if key in patch:
                        t[key] = _as_int(patch[key], t.get(key, 0))
                if "enabled" in patch:
                    t["enabled"] = bool(patch["enabled"])
                if "text_fields" in patch:
                    t["text_fields"] = _norm_text_fields(patch["text_fields"])
                t["updated_at"] = time.time()
                self._validate_snapshot()
                self._save_locked()
                return copy.deepcopy(t)
        raise KeyError("模板不存在")

    def delete_template(self, tpl_id: str) -> None:
        with self._lock:
            before = len(self._templates)
            self._templates = [t for t in self._templates if t["id"] != tpl_id]
            if len(self._templates) == before:
                raise KeyError("模板不存在")
            self._save_locked()

    def inc_usage(self, tpl_id: str) -> None:
        """任务提交时累计使用次数（后台看数据决定推哪个、砍哪个）。"""
        with self._lock:
            for t in self._templates:
                if t["id"] == tpl_id:
                    t["usage_count"] = int(t.get("usage_count", 0)) + 1
                    self._save_locked()
                    return

    def set_cover(self, tpl_id: str, cover: str) -> Dict[str, Any]:
        """封面上传后回写引用与版本号（版本号用于 CDN 缓存刷新）。"""
        with self._lock:
            for t in self._templates:
                if t["id"] == tpl_id:
                    t["cover"] = cover
                    t["cover_v"] = int(t.get("cover_v", 0)) + 1
                    t["updated_at"] = time.time()
                    self._save_locked()
                    return copy.deepcopy(t)
        raise KeyError("模板不存在")

    # ------------------------------------------------------------------ #
    def _validate_snapshot(self) -> None:
        _validate({"groups": self._groups, "templates": self._templates})

    # 公开接口的精简投影：提示词不下发（那是调教出来的东西）
    def public_templates(self, settings) -> List[Dict[str, Any]]:
        """给小程序的模板列表：解析封面 URL，附带分组名。"""
        group_names = {g["id"]: g["name"] for g in self.list_groups(enabled_only=True)}
        out = []
        for t in self.list_templates(enabled_only=True):
            out.append({
                "id": t["id"],
                "group_id": t["group_id"],
                "group_name": group_names.get(t["group_id"], ""),
                "name": t["name"],
                "subtitle": t.get("subtitle", ""),
                "cover": resolve_cover(t, settings),
                "engine": t["engine"],
                "price": int(t.get("price", 0)),
                "layout": t.get("layout", ""),
                "text_fields": t.get("text_fields", []),
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


def resolve_cover(t: Dict[str, Any], settings) -> str:
    """把 cover 引用解析成可访问 URL。

    - http(s) 开头：外链/CDN，原样返回；
    - cos: 前缀：现场预签名（纯 HMAC 计算，零网络请求，2 小时有效），
      对象键自带版本号（covers/{id}_v{n}.jpg），图片流量走 COS/CDN；
    - local: 前缀：后端 /api/covers/ 本地服务（开发期用），?v= 刷缓存；
    - 空：交由前端用分组占位色块兜底。
    """
    cover = str(t.get("cover") or "").strip()
    version = t.get("cover_v", 0)
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
