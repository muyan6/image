# -*- coding: utf-8 -*-
"""运行时设置与公告存储。

设置保存在 backend/data/settings.json,公告在 backend/data/announcements.json,
都是原子写(临时文件 + os.replace),损坏时自动把坏文件改名备份并回退默认值。

设计原则:
- 读线程安全:快照深拷贝,外面拿到手随便用;
- 写串行化:一把锁管全部写入,写入即落盘;
- 后台改配置立刻生效 —— main.py 每次取任务都从 store 现读,不缓存旧值。

密钥明文存在 settings.json 里,待遇等同 .env:data/ 已加入 .gitignore。
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
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger("rescue.settings")

PROVIDER_IDS = ("worldcodes", "fal", "baidu", "local")
SECRET_FIELDS = {
    "worldcodes": ("api_key",),
    "fal": ("api_key",),
    "baidu": ("api_key", "secret_key"),
    "local": (),
}

DEFAULT_LIGHT_PROMPT = (
    "对这张照片做克制、自然的优化：只修复明显的缺陷（模糊、噪点、曝光不准、偏色），"
    "适当提升清晰度与通透感。色彩必须忠实于原始色调与真实氛围：不要过度饱和，"
    "不要浓艳的滤镜感，不要HDR风格，天空不要过度发蓝，植物保持真实颜色。"
    "严格保持画面内容、构图与人物特征不变，不添加或删除物体，不加文字与水印。"
)
DEFAULT_FINE_PROMPT = (
    "以专业修图师的标准对这张照片做自然的高级优化：修复曝光、白平衡、噪点与瑕疵，"
    "提升清晰度与细节层次，呈现通透、真实的质感。"
    "色彩保持克制与忠实——保留原始色调倾向与氛围，避免过度饱和、避免浓艳滤镜与HDR感。"
    "保持主体、构图与人物特征可辨认，不添加文字与水印。"
)

DEFAULT_SETTINGS: Dict[str, Any] = {
    "version": 1,
    "maintenance": {"enabled": False, "message": "服务维护中，稍后再来喵。"},
    "providers": {
        "worldcodes": {
            "enabled": True,
            "base_url": "https://worldcodes.online",
            "api_key": "",
            "endpoint": "/v1/images/edits",
            "model_light": "gpt-image-2.5",
            "model_fine": "gemini-3.1-flash-image-preview",
            "price_light_cny": 0.04,
            "price_fine_cny": 0.15,
            "timeout": 180,
        },
        "fal": {"enabled": True, "api_key": ""},
        "baidu": {"enabled": True, "api_key": "", "secret_key": ""},
        "local": {"enabled": True},
    },
    "chain": ["worldcodes", "fal", "baidu", "local"],
    "prompts": {"light": DEFAULT_LIGHT_PROMPT, "fine": DEFAULT_FINE_PROMPT},
    "prices": {"light": 40, "fine": 40},
    "pricing_revision": 2,
    "free_mode": False,
    "wechat": {"app_id": "", "app_secret": ""},
    "payment": {"offer_id": "", "sandbox_app_key": "", "production_app_key": ""},
    "tencent": {"secret_id": "", "secret_key": "", "cos_bucket": "", "cos_region": "ap-guangzhou", "cos_custom_domain": ""},
    "moderation": {"enabled": False, "block_on_error": False, "wechat_push_token": ""},
    "quota": {"daily": 20, "per_minute": 3},
    "quality_to_style": {"light": "clear", "fine": "gym_contrast"},
    "styles": [
        {"id": "ai", "name": "商业级 AI 超清",
         "desc": "深度学习重构，高保真去噪去模糊", "tag": "AI"},
        {"id": "fuji", "name": "富士经典",
         "desc": "微暖复古，温润通透，人像与街拍百搭", "tag": "胶片"},
        {"id": "clear", "name": "冷白通透",
         "desc": "去黄除暗，高光清爽，极简冷白皮质感", "tag": "热门"},
        {"id": "gym_contrast", "name": "力量高反差",
         "desc": "强化肌肉轮廓与边缘线条，低饱和冷黑金", "tag": "运动"},
    ],
    "normalize_long_side": 1536,
    # 灵感沙龙（社区）：默认空 + 关闭。
    # 内容全部由后台维护，小程序端不再内置任何测试展品。
    "community": {"enabled": False, "items": []},
    # 激励视频广告位：ad_unit_id 留空 = 未开通，小程序端隐藏入口且不发光子。
    # 广告位 ID 在微信公众平台创建后填到这里（后台 /admin 通用设置）。
    "ads": {"rewarded_video_enabled": False, "rewarded_video_unit_id": "",
            "rewarded_video_verifier_url": "", "rewarded_video_verifier_key": ""},
}


def _deep_merge(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    """递归合并 patch 到 base(返回新字典);patch 里 value 为 None 表示删除该键。"""
    out = copy.deepcopy(base)
    for key, value in patch.items():
        if value is None:
            out.pop(key, None)
        elif isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


# 误删后服务会残废的关键结构：合并后一律用默认值兜底补齐
_DICT_SECTIONS = ("providers", "wechat", "payment", "tencent", "moderation", "quota",
                  "prompts", "prices", "maintenance", "quality_to_style",
                  "community", "ads")


def _rebase_defaults(candidate: Dict[str, Any]) -> None:
    """patch 里 value 为 None 会删掉整段配置（如 providers.baidu），
    这里在校验前把缺的关键节点从默认值补回来，保证服务永远可跑。"""
    for key in _DICT_SECTIONS:
        default_val = copy.deepcopy(DEFAULT_SETTINGS.get(key, {}))
        current = candidate.get(key)
        if not isinstance(current, dict):
            candidate[key] = default_val
        else:
            candidate[key] = _deep_merge(default_val, current)
    if not (isinstance(candidate.get("chain"), list) and candidate["chain"]):
        candidate["chain"] = list(DEFAULT_SETTINGS["chain"])
    if not (isinstance(candidate.get("styles"), list) and candidate["styles"]):
        candidate["styles"] = copy.deepcopy(DEFAULT_SETTINGS["styles"])


def _validate(doc: Dict[str, Any]) -> None:
    """整档校验,不合法直接抛 ValueError,调用方放弃本次写入。"""
    chain = doc.get("chain")
    if not isinstance(chain, list) or not chain:
        raise ValueError("chain 不能为空")
    seen = set()
    for name in chain:
        if name not in PROVIDER_IDS:
            raise ValueError("chain 里有未知供应商: %r" % name)
        if name in seen:
            raise ValueError("chain 里 %s 重复" % name)
        seen.add(name)

    providers = doc.get("providers")
    if not isinstance(providers, dict):
        raise ValueError("providers 必须是对象")
    wc = providers.get("worldcodes", {})
    base_url = str(wc.get("base_url", "")).strip()
    if base_url and not re.match(r"^https?://[^\s]+$", base_url):
        raise ValueError("worldcodes.base_url 必须是 http(s):// 开头的完整地址")
    endpoint = str(wc.get("endpoint", "")).strip()
    if endpoint and not endpoint.startswith("/"):
        raise ValueError("worldcodes.endpoint 必须以 / 开头")
    for field in ("price_light_cny", "price_fine_cny"):
        value = wc.get(field, 0)
        if not isinstance(value, (int, float)) or value < 0 or value > 1000:
            raise ValueError("worldcodes.%s 必须是 0~1000 的数字" % field)
    timeout = wc.get("timeout", 180)
    if not isinstance(timeout, int) or not 10 <= timeout <= 600:
        raise ValueError("worldcodes.timeout 必须是 10~600 的整数(秒)")

    prices = doc.get("prices", {})
    for tier in ("light", "fine"):
        value = prices.get(tier)
        if not isinstance(value, int) or not 0 <= value <= 9999:
            raise ValueError("prices.%s 必须是 0~9999 的整数(光子)" % tier)

    if not isinstance(doc.get("free_mode"), bool):
        raise ValueError("free_mode 必须是布尔值")

    for section in ("wechat", "tencent", "moderation"):
        if not isinstance(doc.get(section), dict):
            raise ValueError("%s 必须是对象" % section)
    wx = doc.get("wechat", {})
    for field in ("app_id", "app_secret"):
        if not isinstance(wx.get(field, ""), str) or len(wx.get(field, "")) > 128:
            raise ValueError("wechat.%s 必须是不超过 128 字的文本" % field)
    payment = doc.get("payment", {})
    for field in ("offer_id", "sandbox_app_key", "production_app_key"):
        if not isinstance(payment.get(field, ""), str) or len(payment.get(field, "")) > 200:
            raise ValueError("payment.%s 必须是不超过 200 字的文本" % field)
    tc = doc.get("tencent", {})
    for field in ("secret_id", "secret_key", "cos_bucket", "cos_region", "cos_custom_domain"):
        if not isinstance(tc.get(field, ""), str) or len(tc.get(field, "")) > 200:
            raise ValueError("tencent.%s 必须是不超过 200 字的文本" % field)
    mod = doc.get("moderation", {})
    for field in ("enabled", "block_on_error"):
        if not isinstance(mod.get(field), bool):
            raise ValueError("moderation.%s 必须是布尔值" % field)
    if not isinstance(mod.get("wechat_push_token", ""), str) \
            or len(mod.get("wechat_push_token", "")) > 128:
        raise ValueError("moderation.wechat_push_token 必须是不超过 128 字的文本")
    quota = doc.get("quota", {})
    for field, top in (("daily", 100000), ("per_minute", 1000)):
        value = quota.get(field)
        if not isinstance(value, int) or not 0 <= value <= top:
            raise ValueError("quota.%s 必须是 0~%d 的整数" % (field, top))

    prompts = doc.get("prompts", {})
    for tier in ("light", "fine"):
        text = prompts.get(tier)
        if not isinstance(text, str) or len(text) > 4000:
            raise ValueError("prompts.%s 必须是不超过 4000 字的文本" % tier)

    normalize = doc.get("normalize_long_side")
    if not isinstance(normalize, int) or not 0 <= normalize <= 8192:
        raise ValueError("normalize_long_side 必须是 0~8192 的整数,0 为关闭")

    styles = doc.get("styles", [])
    if not isinstance(styles, list) or not styles:
        raise ValueError("styles 不能为空")
    ids = set()
    for item in styles:
        if not isinstance(item, dict) or not item.get("id"):
            raise ValueError("styles 每项都要有 id")
        if item["id"] in ids:
            raise ValueError("styles id 重复: %r" % item["id"])
        ids.add(item["id"])

    for tier, style_id in doc.get("quality_to_style", {}).items():
        if style_id not in {None, ""} | ids:
            raise ValueError("quality_to_style.%s 指向不存在的风格 %r" % (tier, style_id))

    maintenance = doc.get("maintenance", {})
    if not isinstance(maintenance.get("enabled"), bool):
        raise ValueError("maintenance.enabled 必须是布尔值")
    if not isinstance(maintenance.get("message", ""), str):
        raise ValueError("maintenance.message 必须是文本")

    community = doc.get("community", {})
    if not isinstance(community.get("enabled"), bool):
        raise ValueError("community.enabled 必须是布尔值")
    items = community.get("items", [])
    if not isinstance(items, list):
        raise ValueError("community.items 必须是数组")
    if len(items) > 200:
        raise ValueError("community.items 最多 200 条")
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError("community.items[%d] 必须是对象" % idx)
        likes = item.get("likes", 0)
        if type(likes) is not int or not 0 <= likes <= 1_000_000_000:
            raise ValueError("community.items[%d].likes 必须是非负整数" % idx)
        for field in ("title", "story", "template_name"):
            if not isinstance(item.get(field, ""), str):
                raise ValueError("community.items[%d].%s 必须是文本" % (idx, field))
        for field in ("result_url", "orig_url"):
            value = item.get(field, "")
            if not isinstance(value, str) or len(value) > 1000:
                raise ValueError("community.items[%d].%s 必须是不超过 1000 字的文本"
                                 % (idx, field))

    ads = doc.get("ads", {})
    if not isinstance(ads.get("rewarded_video_enabled"), bool):
        raise ValueError("ads.rewarded_video_enabled 必须是布尔值")
    unit_id = ads.get("rewarded_video_unit_id", "")
    if not isinstance(unit_id, str) or len(unit_id) > 64:
        raise ValueError("ads.rewarded_video_unit_id 必须是不超过 64 字的文本")
    verifier_url = ads.get("rewarded_video_verifier_url", "")
    if not isinstance(verifier_url, str) or len(verifier_url) > 1000 or \
            (verifier_url and not re.fullmatch(r"https://[^\s]+", verifier_url)):
        raise ValueError("广告验证服务必须是 HTTPS 地址")
    verifier_key = ads.get("rewarded_video_verifier_key", "")
    if not isinstance(verifier_key, str) or len(verifier_key) > 256:
        raise ValueError("广告验证服务密钥最多 256 字")


class SettingsStore:
    """线程安全的运行时设置。"""

    def __init__(self, data_dir: str,
                 mutate_default: Optional[Callable[[Dict[str, Any]], None]] = None) -> None:
        self._path = os.path.join(data_dir, "settings.json")
        self._lock = threading.RLock()
        os.makedirs(data_dir, exist_ok=True)

        defaults = copy.deepcopy(DEFAULT_SETTINGS)
        if mutate_default is not None:
            # 首次落盘前从环境变量迁移密钥等一次性操作
            mutate_default(defaults)

        self._data = defaults
        self._load_or_init(defaults)

    # ------------------------------------------------------------------ #
    # 持久化
    # ------------------------------------------------------------------ #
    def _load_or_init(self, defaults: Dict[str, Any]) -> None:
        if not os.path.exists(self._path):
            self._save_locked(defaults)
            log.info("设置初始化: %s", self._path)
            return
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if not isinstance(loaded, dict):
                raise ValueError("根节点不是对象")
        except (OSError, ValueError) as exc:
            stamp = time.strftime("%Y%m%d_%H%M%S")
            backup = "%s.corrupt_%s" % (self._path, stamp)
            try:
                os.replace(self._path, backup)
                log.error("设置文件损坏(%s),已备份到 %s 并回退默认值", exc, backup)
            except OSError:
                log.error("设置文件损坏(%s),备份失败,直接回退默认值", exc)
            self._data = copy.deepcopy(defaults)
            self._save_locked(self._data)
            return
        # 深合并:文件里缺的新字段用默认补齐,未知字段保留
        self._data = _deep_merge(defaults, loaded)
        if int(loaded.get("pricing_revision") or 0) < 2:
            # 一次性将旧站 1/3 光子档位统一切到 40，保留其他运行设置。
            self._data["prices"] = {"light": 40, "fine": 40}
            self._data["pricing_revision"] = 2
            self._save_locked(self._data)
        log.info("设置已加载: %s", self._path)

    def _save_locked(self, doc: Dict[str, Any]) -> None:
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, self._path)

    # ------------------------------------------------------------------ #
    # 读写
    # ------------------------------------------------------------------ #
    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data)

    def update(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        """递归合并 patch,关键节点兜底补齐,校验通过才落盘。返回更新后的完整快照。"""
        with self._lock:
            candidate = _deep_merge(self._data, patch)
            _rebase_defaults(candidate)
            _validate(candidate)
            self._save_locked(candidate)
            self._data = candidate
            return copy.deepcopy(self._data)

    def mutate_community(self, mutate) -> Dict[str, Any]:
        """Atomic post edits: another settings save cannot overwrite a read snapshot."""
        with self._lock:
            community = copy.deepcopy(self._data.get("community") or {"enabled": True, "items": []})
            mutate(community)
            candidate = copy.deepcopy(self._data)
            candidate["community"] = community
            _validate(candidate)
            self._save_locked(candidate)
            self._data = candidate
            return copy.deepcopy(community)

    # ------------------------------------------------------------------ #
    # 常用读取便捷方法(全部现读,保证后台改动立即生效)
    # ------------------------------------------------------------------ #
    def chain(self) -> List[str]:
        return list(self.snapshot()["chain"])

    def provider(self, name: str) -> Dict[str, Any]:
        return copy.deepcopy(self.snapshot()["providers"].get(name, {}))

    def provider_enabled(self, name: str) -> bool:
        conf = self.provider(name)
        return bool(conf.get("enabled", name == "local"))

    def prompt_for(self, quality: str) -> str:
        prompts = self.snapshot()["prompts"]
        return prompts.get(quality) or prompts.get("fine") or DEFAULT_FINE_PROMPT

    def prices(self) -> Dict[str, int]:
        return copy.deepcopy(self.snapshot()["prices"])

    def free_mode(self) -> bool:
        """调试模式：小程序读到 true 就跳过小鱼干扣减，次数不限。"""
        return bool(self.snapshot().get("free_mode"))

    def wechat(self) -> Dict[str, str]:
        return copy.deepcopy(self.snapshot()["wechat"])

    def tencent(self) -> Dict[str, str]:
        return copy.deepcopy(self.snapshot()["tencent"])

    def moderation(self) -> Dict[str, Any]:
        return copy.deepcopy(self.snapshot()["moderation"])

    def quota(self) -> Dict[str, int]:
        return copy.deepcopy(self.snapshot()["quota"])

    def cos_ready(self) -> bool:
        """COS 直传是否可用：密钥与桶信息齐全。"""
        tc = self.tencent()
        return bool(tc.get("secret_id") and tc.get("secret_key")
                    and tc.get("cos_bucket") and tc.get("cos_region"))

    def moderation_ready(self) -> bool:
        mod = self.moderation()
        tc = self.tencent()
        return bool(mod.get("enabled") and tc.get("secret_id") and tc.get("secret_key"))

    def maintenance(self) -> Dict[str, Any]:
        return copy.deepcopy(self.snapshot()["maintenance"])

    def normalize_long_side(self) -> int:
        return int(self.snapshot()["normalize_long_side"])

    def styles(self) -> List[Dict[str, Any]]:
        return copy.deepcopy(self.snapshot()["styles"])

    def quality_to_style(self) -> Dict[str, str]:
        return copy.deepcopy(self.snapshot()["quality_to_style"])

    def community(self) -> Dict[str, Any]:
        """灵感沙龙配置：{enabled: bool, items: [...]}。默认关闭且为空。"""
        conf = self.snapshot().get("community") or {}
        if not isinstance(conf, dict):
            return {"enabled": False, "items": []}
        items = conf.get("items")
        return {
            "enabled": bool(conf.get("enabled")),
            "items": [copy.deepcopy(item) for item in items[:200] if isinstance(item, dict)]
            if isinstance(items, list) else [],
        }

    def ads(self) -> Dict[str, Any]:
        """激励视频广告配置。enabled 且 unit_id 非空才算真的开通。"""
        conf = self.snapshot().get("ads") or {}
        if not isinstance(conf, dict):
            conf = {}
        unit_id = str(conf.get("rewarded_video_unit_id") or "").strip()
        verifier_url = str(conf.get("rewarded_video_verifier_url") or "").strip()
        verifier_key = str(conf.get("rewarded_video_verifier_key") or "").strip()
        return {
            "rewarded_video_enabled": bool(conf.get("rewarded_video_enabled")),
            "rewarded_video_unit_id": unit_id,
            "rewarded_video_ready": bool(conf.get("rewarded_video_enabled")) and bool(unit_id)
            and verifier_url.startswith("https://") and bool(verifier_key),
            "rewarded_video_verifier_url": verifier_url,
            "rewarded_video_verifier_key": verifier_key,
        }


class AnnouncementStore:
    """公告列表:后台 CRUD,小程序只读启用的。"""

    LEVELS = ("info", "warn", "critical")

    def __init__(self, data_dir: str) -> None:
        self._path = os.path.join(data_dir, "announcements.json")
        self._lock = threading.RLock()
        os.makedirs(data_dir, exist_ok=True)
        self._items: List[Dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self._path):
            self._save_locked()
            return
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, list):
                self._items = [item for item in loaded if isinstance(item, dict)]
            else:
                raise ValueError("根节点不是列表")
        except (OSError, ValueError) as exc:
            log.error("公告文件损坏(%s),已重置", exc)
            self._items = []
            self._save_locked()

    def _save_locked(self) -> None:
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._items, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, self._path)

    def list_all(self) -> List[Dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._items)

    def list_enabled(self, limit: int = 10) -> List[Dict[str, Any]]:
        with self._lock:
            items = [copy.deepcopy(i) for i in self._items if i.get("enabled")]
        items.sort(key=lambda i: i.get("created_at", 0), reverse=True)
        return [
            {k: item[k] for k in ("id", "title", "body", "level", "created_at")}
            for item in items[:limit]
        ]

    def create(self, title: str, body: str, level: str = "info",
               enabled: bool = True) -> Dict[str, Any]:
        if level not in self.LEVELS:
            raise ValueError("level 只能是 %s" % "/".join(self.LEVELS))
        title = (title or "").strip()
        body = (body or "").strip()
        if not title or len(title) > 60:
            raise ValueError("标题必填且不超过 60 字")
        if not body or len(body) > 2000:
            raise ValueError("正文必填且不超过 2000 字")
        item = {
            "id": uuid.uuid4().hex[:12],
            "title": title,
            "body": body,
            "level": level,
            "enabled": bool(enabled),
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        with self._lock:
            self._items.append(item)
            self._save_locked()
        return copy.deepcopy(item)

    def update(self, item_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            for item in self._items:
                if item["id"] == item_id:
                    if "title" in patch:
                        title = (patch["title"] or "").strip()
                        if not title or len(title) > 60:
                            raise ValueError("标题必填且不超过 60 字")
                        item["title"] = title
                    if "body" in patch:
                        body = (patch["body"] or "").strip()
                        if not body or len(body) > 2000:
                            raise ValueError("正文必填且不超过 2000 字")
                        item["body"] = body
                    if "level" in patch:
                        if patch["level"] not in self.LEVELS:
                            raise ValueError("level 非法")
                        item["level"] = patch["level"]
                    if "enabled" in patch:
                        item["enabled"] = bool(patch["enabled"])
                    item["updated_at"] = time.time()
                    self._save_locked()
                    return copy.deepcopy(item)
        raise KeyError("公告不存在")

    def delete(self, item_id: str) -> None:
        with self._lock:
            before = len(self._items)
            self._items = [i for i in self._items if i["id"] != item_id]
            if len(self._items) == before:
                raise KeyError("公告不存在")
            self._save_locked()
