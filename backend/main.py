# -*- coding: utf-8 -*-
"""
废片拯救后端服务 (Photo Rescue API)

架构
----
    小程序 / 网页
        |  POST /api/rescue          提交，立刻拿到 job_id
        |  GET  /api/jobs/{id}       轮询状态
        |  GET  /api/images/{name}   取图
        v
    FastAPI + 线程池
        |
        +-- 归一化（长边 1536，对齐原站行为）
        +-- AI 增强：fal.ai  ->  baidu  ->  本地 engine.py

密钥全部走环境变量，代码里不出现任何 AK/SK。
见 backend/.env.example。
"""
from __future__ import annotations

import hashlib
import glob
import json
import logging
import mimetypes
import os
import re
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from admin_api import ensure_admin_password, make_admin_router
from cos_store import (CosError, get_object as cos_get,
                       head_exists as cos_head, presign as cos_presign,
                       put_object as cos_put)
from engine import ImageRescueEngine
from gateway_ai import OpenAIImagesEnhance
from settings_store import AnnouncementStore, SettingsStore
from templates_store import TemplateStore, covers_dir
from text_overlay import apply as apply_text_overlay, collect_values
from tencent_cs import ModerationError, moderate_image_bytes
from user_store import UserStore
import wechat_sec
from wechat_sec import WechatSecError
from wechat_auth import (WechatAuthError, bearer_of, code2session,
                         make_token as user_token, verify_token as verify_user_token)
from fal_ai import FalImageEnhance

try:
    from baidu_ai import BaiduImageEnhance
except Exception:  # pragma: no cover
    BaiduImageEnhance = None  # type: ignore

# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("rescue")


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.environ.get("UPLOAD_DIR") or os.path.join(BASE_DIR, "uploads")
LUT_DIR = os.path.join(BASE_DIR, "luts")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(LUT_DIR, exist_ok=True)


def _load_dotenv_early() -> None:
    """启动即加载 backend/.env（override=False，真实环境变量优先）。

    ADMIN_PASSWORD / FAL_KEY / BAIDU_* 都写在 .env 里，必须先于
    ensure_admin_password() 和设置迁移读取，否则每次启动都会
    误判"未配置密码"而重新生成覆盖。
    """
    env_path = os.path.join(BASE_DIR, ".env")
    try:
        from dotenv import load_dotenv
        load_dotenv(env_path, override=False)
        return
    except ImportError:
        pass
    if not os.path.exists(env_path):
        return
    with open(env_path, "r", encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


_load_dotenv_early()

# 上传限制
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", 25 * 1024 * 1024))
MAX_PIXELS = int(os.environ.get("MAX_PIXELS", 60_000_000))
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}

# 任务保留
JOB_TTL_SECONDS = int(os.environ.get("JOB_TTL_SECONDS", 30 * 24 * 3600))
JOB_MAX_ENTRIES = int(os.environ.get("JOB_MAX_ENTRIES", 5000))

# 每日奖励（服务端记账，客户端只是展示）
EARN_DEFS: Dict[str, Dict[str, Any]] = {
    "checkin": {"reward": 10, "limit": 1, "label": "签到"},
    "video": {"reward": 10, "limit": 3, "label": "看视频"},
}

# 归一化长边改由后台设置热调（settings.normalize_long_side()），
# 环境变量 NORMALIZE_LONG_SIDE 仅作为首次初始化的默认值。

# 线程池：cv2 / 网络 IO 都会阻塞，别占住事件循环
WORKERS = int(os.environ.get("WORKERS", 4))

# CORS：默认只放行本机与微信开发者工具常用来源。
# 需要放开时用环境变量覆盖，逗号分隔；填 * 表示全放行（但不带 cookie）。
_origins = os.environ.get("ALLOWED_ORIGINS", "*").strip()
ALLOWED_ORIGINS = [o.strip() for o in _origins.split(",") if o.strip()] or ["*"]
ALLOW_CREDENTIALS = "*" not in ALLOWED_ORIGINS


# --------------------------------------------------------------------------- #
# 应用
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(_app: FastAPI):
    """启动/关闭钩子。用 lifespan 替代已废弃的 on_event。"""
    ensure_admin_password()  # 没配 ADMIN_PASSWORD 时生成随机密码写回 .env 并打印
    log.info("上传目录: %s", UPLOAD_DIR)
    log.info("归一化长边: %s", settings.normalize_long_side() or "关闭")
    log.info("降级链: %s", " -> ".join(settings.chain()))
    h = health()
    log.info(
        "AI 供应商: 中转网关=%s fal.ai=%s 百度=%s（密钥与开关在 /admin 后台管理）",
        h["gateway"], h["fal"], h["baidu"],
    )
    if not h["configured"]:
        log.warning("没有任何 AI 后端，全部走本地 engine.py")
    _startup_file_gc()
    threading.Thread(target=_bg_sweeper, name="job-sweeper",
                     daemon=True).start()
    yield
    pool.shutdown(wait=False, cancel_futures=True)
    log.info("已停止")


app = FastAPI(
    title="废片拯救后端服务 (Photo Rescue API)",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    # 注意：allow_origins=["*"] 与 allow_credentials=True 是非法组合，
    # 浏览器会直接拒绝。这里二者互斥。
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=ALLOW_CREDENTIALS,
    allow_methods=["*"],
    allow_headers=["*"],
)

engine = ImageRescueEngine(lut_dir=LUT_DIR)


# --------------------------------------------------------------------------- #
# 运行时设置 + 公告（backend/data/*.json，后台 /admin 可热改）
# --------------------------------------------------------------------------- #
DATA_DIR = os.path.join(BASE_DIR, "data")


def _migrate_env_keys(doc: Dict[str, Any]) -> None:
    """首次初始化 settings.json 时，把 .env 里的密钥迁移进去；此后以后台为准。"""
    fal_key = os.environ.get("FAL_KEY", "").strip()
    if fal_key and not doc["providers"]["fal"].get("api_key"):
        doc["providers"]["fal"]["api_key"] = fal_key
    bd_ak = os.environ.get("BAIDU_API_KEY", "").strip()
    bd_sk = os.environ.get("BAIDU_SECRET_KEY", "").strip()
    if bd_ak and not doc["providers"]["baidu"].get("api_key"):
        doc["providers"]["baidu"]["api_key"] = bd_ak
    if bd_sk and not doc["providers"]["baidu"].get("secret_key"):
        doc["providers"]["baidu"]["secret_key"] = bd_sk
    wx_appid = os.environ.get("WX_APPID", "").strip()
    if wx_appid and not doc["wechat"].get("app_id"):
        doc["wechat"]["app_id"] = wx_appid
    wx_secret = os.environ.get("WX_APP_SECRET", "").strip()
    if wx_secret and not doc["wechat"].get("app_secret"):
        doc["wechat"]["app_secret"] = wx_secret
    tc_id = os.environ.get("TENCENT_SECRET_ID", "").strip()
    if tc_id and not doc["tencent"].get("secret_id"):
        doc["tencent"]["secret_id"] = tc_id
    tc_key = os.environ.get("TENCENT_SECRET_KEY", "").strip()
    if tc_key and not doc["tencent"].get("secret_key"):
        doc["tencent"]["secret_key"] = tc_key
    if os.environ.get("NORMALIZE_LONG_SIDE"):
        try:
            doc["normalize_long_side"] = int(os.environ["NORMALIZE_LONG_SIDE"])
        except ValueError:
            pass


settings = SettingsStore(DATA_DIR, mutate_default=_migrate_env_keys)
announcements = AnnouncementStore(DATA_DIR)
templates = TemplateStore(DATA_DIR)
users = UserStore(DATA_DIR)


# --------------------------------------------------------------------------- #
# 供应商客户端工厂：配置变了自动重建（后台改密钥/模型即时生效）
# --------------------------------------------------------------------------- #
_clients: Dict[str, Any] = {}
_clients_lock = threading.Lock()


def _fingerprint(conf: Dict[str, Any]) -> int:
    return hash(json.dumps(conf, sort_keys=True, ensure_ascii=False))


def _build_client(name: str, conf: Dict[str, Any]):
    if name == "worldcodes":
        return OpenAIImagesEnhance(conf)
    if name == "fal":
        key = str(conf.get("api_key") or "").strip()
        if not key and not os.environ.get("FAL_KEY", "").strip():
            return None
        return FalImageEnhance(api_key=key or None)
    if name == "baidu":
        ak = str(conf.get("api_key") or "").strip()
        sk = str(conf.get("secret_key") or "").strip()
        if not ak or not sk:
            return None
        try:
            return BaiduImageEnhance(api_key=ak, secret_key=sk)
        except ValueError:
            return None
    return None


def _get_client(name: str):
    """按当前设置取客户端；配置指纹变化时重建。返回 None 表示不可用。

    构建动作很轻（只是 new 一个 HTTP 客户端对象），直接在锁内完成，
    避免两个线程同时构建互相覆盖。
    """
    conf = settings.provider(name)
    fp = _fingerprint(conf)
    with _clients_lock:
        cached = _clients.get(name)
        if cached is not None and cached[0] == fp:
            return cached[1]
        client = _build_client(name, conf)
        _clients[name] = (fp, client)
        return client


pool = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="rescue")


# --------------------------------------------------------------------------- #
# 用户鉴权 / 直传登记 / 配额 / 审核
# --------------------------------------------------------------------------- #
def _current_user(request) -> Dict[str, Any]:
    """从 Authorization: Bearer 解出 openid 并返回用户行，未登录抛 401。

    已注册用户走纯读（轮询接口每 1.5s 打一次，不能每次都写库）。
    """
    token = bearer_of({k.lower(): v for k, v in request.headers.items()})
    openid = verify_user_token(token) if token else None
    if not openid:
        raise HTTPException(status_code=401, detail="请先登录")
    return users.get_user(openid) or users.ensure_user(openid)


# 直传登记：upload_id -> 登记，1 小时未完成自动作废
_uploads: Dict[str, Dict[str, Any]] = {}
_uploads_lock = threading.Lock()


def _sweep_uploads_locked() -> None:
    cutoff = time.time() - 3600
    for uid in [k for k, v in _uploads.items() if v["created_at"] < cutoff]:
        _uploads.pop(uid, None)


def _check_quota(openid: str) -> Optional[str]:
    """频控 + 每日配额。返回拒绝原因，None = 放行。free_mode 只免每日配额。"""
    quota = settings.quota()
    now = time.time()
    if quota.get("per_minute", 0) > 0 and \
            users.jobs_in_window(openid, now - 60) >= quota["per_minute"]:
        users.audit(openid, "rate_limited")
        return "提交太频繁，歇一会儿再来喵"
    if quota.get("daily", 0) > 0 and not settings.free_mode():
        lt = time.localtime(now)
        midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
        if users.jobs_in_window(openid, midnight) >= quota["daily"]:
            users.audit(openid, "quota_exhausted")
            return "今日次数已用完，明天再来吧"
    return None


_WX_SEC_LABELS = {
    "100": "正常",
    "10001": "广告导流",
    "20001": "时政敏感",
    "20002": "色情低俗",
    "20003": "辱骂恶俗",
    "20006": "违禁违法",
    "20008": "欺诈骗局",
    "20012": "低俗不良",
    "20013": "版权侵权",
    "21000": "其他违规",
    "Porn": "色情低俗",
    "Terror": "暴恐违禁",
    "Polity": "时政敏感",
    "Politics": "时政敏感",
    "Disgusting": "令人不适",
}


def _sec_label_desc(label: Any) -> str:
    s = str(label or "").strip()
    return _WX_SEC_LABELS.get(s, s)


def _moderate_or_reject(image_bytes: bytes, job_ctx: str, openid: str) -> Optional[str]:
    """内容审核（分级省钱）。返回拒绝原因，None = 放行。

    - ≤10M 且微信免费审核可用（机审开启 + 微信密钥 + COS + 推送 Token 齐全）
      -> mediaCheckAsync：免费，异步接口，同步等待微信推送 1~3 秒（上限 12 秒）；
    - 否则若腾讯云密钥齐全 -> 腾讯云 IMS（≈ ¥0.0015/张，同步 1~2 秒）；
    - 两级都不可用：放行（机审开关本身关闭时也放行）。

    微信 suggest pass/review/risky 与腾讯 Pass/Review/Block 映射为同一判定：
    非 pass/Pass 一律拦截。审核服务异常按 block_on_error 策略（默认放行大声记日志）。
    """
    mod = settings.moderation()
    if not mod.get("enabled"):
        return None

    suggestion = label = score = None
    if wechat_sec.wechat_sec_ready(settings) \
            and len(image_bytes) <= wechat_sec.MAX_WECHAT_CHECK_BYTES:
        try:
            suggestion, label, score = wechat_sec.check_image(
                settings, image_bytes, openid)
            log.info("[%s] 微信免费审核完成: %s/%s/%s", job_ctx, suggestion, label, score)
        except WechatSecError as exc:
            log.warning("[%s] 微信免费审核不可用(%s)，回退腾讯云: %s",
                        job_ctx, exc.code, exc)
            suggestion = None

    if suggestion is None:
        if not settings.moderation_ready():
            if mod.get("block_on_error"):
                users.audit(openid, "moderation_unconfigured", "机审已开启但未就绪")
                return "安全审核服务未就绪，已按安全策略拦截"
            return None  # 腾讯云也没配密钥：放行
        try:
            suggestion, label, score = moderate_image_bytes(image_bytes, settings)
        except ModerationError as exc:
            if mod.get("block_on_error"):
                users.audit(openid, "moderation_error", str(exc))
                return "安全审核服务异常，已按策略拦截"
            log.warning("[%s] 审核服务异常(%s)，本次放行: %s", job_ctx, exc.code, exc)
            return None

    if str(suggestion).lower() != "pass":
        users.audit(openid, "blocked",
                    "label=%s score=%s suggest=%s" % (label, score, suggestion))
        users.inc_blocked(openid)
        desc = _sec_label_desc(label)
        return "图片内容未通过安全审核（%s）" % desc if desc and desc != "100" else "图片内容未通过安全审核"
    return None


def _refund_charged(openid: str, job_id: str, price: int) -> None:
    """任务失败/被拦截时全额退还预扣的光子。"""
    if price <= 0 or settings.free_mode():
        return
    try:
        balance = users.add_balance(openid, int(price))
        users.audit(openid, "refund", "job=%s +=%d balance=%d"
                    % (job_id, price, balance))
        log.info("[%s] 已退还 %d 光子（余额 %d）", job_id, price, balance)
    except Exception:  # noqa: BLE001
        log.exception("[%s] 退还光子失败", job_id)


def _crop_aspect_ratio(img: Any, aspect_ratio: str = "") -> Any:
    """按画幅比例做居中裁剪。空或 original/auto 则不裁剪。"""
    if not aspect_ratio or img is None:
        return img
    ar = str(aspect_ratio).strip().lower()
    if ar in ("original", "auto", "none", ""):
        return img

    ratio_map = {
        "1:1": 1.0,
        "3:4": 3.0 / 4.0,
        "4:3": 4.0 / 3.0,
        "9:16": 9.0 / 16.0,
        "16:9": 16.0 / 9.0,
        "2:3": 2.0 / 3.0,
        "3:2": 3.0 / 2.0,
    }
    target_ratio = ratio_map.get(ar)
    if target_ratio is None:
        try:
            parts = ar.split(":")
            if len(parts) == 2:
                target_ratio = float(parts[0]) / float(parts[1])
        except Exception:
            target_ratio = None

    if not target_ratio or target_ratio <= 0:
        return img

    h, w = img.shape[:2]
    if h == 0 or w == 0:
        return img
    cur_ratio = w / float(h)
    if abs(cur_ratio - target_ratio) < 0.008:
        return img

    if cur_ratio > target_ratio:
        new_w = max(1, int(round(h * target_ratio)))
        offset_x = max(0, (w - new_w) // 2)
        return img[:, offset_x:offset_x + new_w]
    else:
        new_h = max(1, int(round(w / target_ratio)))
        offset_y = max(0, (h - new_h) // 2)
        return img[offset_y:offset_y + new_h, :]


def _register_job(openid: str, quality: str, style: str,
                  orig_tmp_path: str, ext: str,
                  template: Optional[Dict[str, Any]] = None,
                  text_values: Optional[Dict[str, str]] = None,
                  aspect_ratio: str = "") -> Dict[str, Any]:
    """把已落盘的原图登记为任务（ multipart 与 COS 直传共用这条尾巴）。

    template 非空时，引擎档位/提示词/输出尺寸/文字排版全部以模板为准，
    价格用模板价（未定价回退到档位默认价）。
    光子在提交时预扣（服务端记账，余额不足直接 402），任务失败自动退款。
    """
    price = _effective_price(quality, template)
    charged = 0
    balance = users.get_balance(openid)
    if not settings.free_mode() and price > 0:
        ok, balance = users.try_spend(openid, price)
        if not ok:
            raise HTTPException(
                status_code=402,
                detail="光子不足：本次需要 %d ✦，当前余额 %d ✦" % (price, balance))
        charged = price

    job_id = uuid.uuid4().hex[:12]
    try:
        orig_file = "orig_%s%s" % (job_id, ext)
        orig_path = os.path.join(UPLOAD_DIR, orig_file)
        shutil.move(orig_tmp_path, orig_path)

        # 若指定了非原图画幅比例裁剪，原图同步按相同画幅居中裁剪，
        # 保证原画与重构图画幅、构图与像素级视差 100% 对齐（彻底消除对比滑块错位重影问题）
        ar = str(aspect_ratio).strip().lower()
        if ar and ar not in ("original", "auto", "none", ""):
            try:
                import cv2
                _img = cv2.imread(orig_path, cv2.IMREAD_COLOR)
                if _img is not None:
                    _cropped = _crop_aspect_ratio(_img, ar)
                    _target_ext = ext if ext.lower() in (".jpg", ".jpeg", ".png") else ".jpg"
                    _ok, _buf = cv2.imencode(_target_ext, _cropped, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
                    if _ok:
                        _buf.tofile(orig_path)
            except Exception as _exc:
                log.warning("[%s] 原图居中裁剪同步失败: %s", job_id, _exc)

        result_file = "result_%s.jpg" % job_id

        if template:
            templates.inc_usage(template["id"])

        orig_cos = result_cos = None
        if settings.cos_ready():
            try:
                orig_cos = "origins/%s/%s%s" % (openid[:8], job_id, ext)
                with open(orig_path, "rb") as fh:
                    cos_put(settings, orig_cos, fh.read())
                result_cos = "results/%s/%s.jpg" % (openid[:8], job_id)
            except CosError as exc:
                log.warning("[%s] COS 登记失败，回退本地存储: %s", job_id, exc)
                orig_cos = result_cos = None

        jobs.create(
            job_id,
            openid=openid,
            quality=quality,
            style=style,
            template_id=(template or {}).get("id", ""),
            template_name=(template or {}).get("name", ""),
            price=price,
            aspect_ratio=aspect_ratio,
            orig_file=orig_file,
            result_file=result_file,
            stage="queued",
            orig_url="/api/images/%s" % orig_file,
            result_url="/api/images/%s" % result_file,
            orig_cos=orig_cos,
            result_cos=result_cos,
        )
    except HTTPException:
        _refund_charged(openid, job_id, charged)
        raise
    except Exception:
        _refund_charged(openid, job_id, charged)
        raise

    jobs.sweep()
    users.inc_total(openid)
    users.audit(openid, "submitted", "job=%s quality=%s tpl=%s ar=%s price=%d"
                % (job_id, quality, (template or {}).get("id", "-"),
                   aspect_ratio or "-", price))
    pool.submit(_run_pipeline, job_id, quality, style,
                copy_template(template), dict(text_values or {}), aspect_ratio)
    orig_url_val = "/api/images/%s" % orig_file
    if orig_cos and settings.cos_ready():
        try:
            orig_url_val = cos_presign(settings, "get", orig_cos, ttl_seconds=7200)
        except Exception:
            pass

    return {
        "code": 0,
        "job_id": job_id,
        "status": "processing",
        "quality": quality,
        "template_id": (template or {}).get("id", ""),
        "template_name": (template or {}).get("name", ""),
        "aspect_ratio": aspect_ratio,
        "price": price,
        "balance": balance,
        "orig_url": orig_url_val,
        "result_url": "/api/images/%s" % result_file,
    }


def _effective_price(quality: str,
                     template: Optional[Dict[str, Any]]) -> int:
    """模板价优先，未定价（0）回退档位默认价。"""
    tpl_price = int((template or {}).get("price", 0) or 0)
    if tpl_price > 0:
        return tpl_price
    return int(settings.prices().get(quality, 0))


def copy_template(template: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """任务管线用的是提交那一刻的模板快照（后台随后改配置不影响在途任务）。"""
    return dict(template) if template else None


# --------------------------------------------------------------------------- #
# 任务存储（进程内，带锁 + TTL 清理）
# --------------------------------------------------------------------------- #
class JobStore:
    """进程内任务表。

    单机轻量调度够用；要横向扩容请换成 Redis。
    驱逐（过期/超量）时通过 on_evict 回调通知上层清理磁盘文件。
    """

    def __init__(self, ttl: int, max_entries: int,
                 on_evict: Optional[Any] = None) -> None:
        self._data: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._ttl = ttl
        self._max = max_entries
        self._on_evict = on_evict

    def create(self, job_id: str, **fields: Any) -> Dict[str, Any]:
        now = time.time()
        job = {
            "id": job_id,
            "status": "processing",
            "created_at": now,
            "updated_at": now,
            "error": None,
            **fields,
        }
        with self._lock:
            self._data[job_id] = job
            evicted: List[Dict[str, Any]] = []
            if len(self._data) > self._max:
                evicted = self._evict_locked(keep=job_id)
        self._notify_evicted(evicted)
        return job

    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            job = self._data.get(job_id)
            return dict(job) if job else None

    def update(self, job_id: str, **fields: Any) -> None:
        with self._lock:
            job = self._data.get(job_id)
            if job is None:
                return
            job.update(fields)
            job["updated_at"] = time.time()

    def _evict_locked(self, keep: Optional[str] = None) -> List[Dict[str, Any]]:
        """先删过期，再按创建时间删最旧。调用方必须持锁。返回被驱逐的任务。"""
        evicted: List[Dict[str, Any]] = []
        now = time.time()
        for jid in [k for k, v in self._data.items()
                    if now - v.get("created_at", now) > self._ttl]:
            evicted.append(self._data.pop(jid))
        while len(self._data) > self._max:
            oldest = min(
                (k for k in self._data if k != keep),
                key=lambda k: self._data[k].get("created_at", 0),
                default=None,
            )
            if oldest is None:
                break
            evicted.append(self._data.pop(oldest))
        return evicted

    def _notify_evicted(self, evicted: List[Dict[str, Any]]) -> None:
        if not evicted or self._on_evict is None:
            return
        try:
            self._on_evict(evicted)
        except Exception:  # noqa: BLE001 —— 清理失败不影响主流程
            log.exception("清理被驱逐任务的文件失败")

    def sweep(self) -> None:
        with self._lock:
            evicted = self._evict_locked()
        self._notify_evicted(evicted)

    def list_recent(self, offset: int = 0, limit: int = 50):
        """后台任务列表：按创建时间倒序。返回 (items, total)。"""
        with self._lock:
            ordered = sorted(
                self._data.values(),
                key=lambda j: j.get("created_at", 0),
                reverse=True,
            )
            return (
                [dict(j) for j in ordered[offset:offset + limit]],
                len(ordered),
            )

    def list_for_openid(self, openid: str, offset: int = 0, limit: int = 50) -> List[Dict[str, Any]]:
        """按 openid 筛选当前用户的任务列表：按创建时间倒序。"""
        with self._lock:
            matched = [
                dict(j) for j in self._data.values()
                if j.get("openid") == openid
            ]
            matched.sort(key=lambda j: j.get("created_at", 0), reverse=True)
            return matched[offset:offset + limit]


def _cleanup_job_files(evicted_jobs: List[Dict[str, Any]]) -> None:
    """任务被驱逐后删除其磁盘文件（只删已出结果/失败的，排队中的不动）。"""
    for job in evicted_jobs:
        if job.get("status") == "processing":
            continue  # 可能还在队列里，等下一轮 TTL 再收
        for field in ("orig_file", "result_file"):
            name = job.get(field)
            if name:
                _safe_remove(os.path.join(UPLOAD_DIR, name))


def _startup_file_gc() -> None:
    """启动时清一次磁盘：临时文件超 1 天、任务产物超 TTL 的直接删。

    任务表在内存里（重启即空），所以不能按"不在表里 = 孤儿"判断，
    一律以文件 mtime 为准 —— TTL 之外的产物本来也不可达了。
    """
    cutoff_tmp = time.time() - 24 * 3600
    cutoff_job = time.time() - JOB_TTL_SECONDS
    removed = 0
    for path in glob.glob(os.path.join(UPLOAD_DIR, "*")):
        base = os.path.basename(path)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if base.startswith(("incoming_", "tmp_", "norm_")):
            stale = mtime < cutoff_tmp
        elif base.startswith(("orig_", "result_")):
            stale = mtime < cutoff_job
        else:
            continue
        if stale and _safe_remove(path):
            removed += 1
    if removed:
        log.info("启动清理：删除 %d 个过期上传/结果文件", removed)


def _bg_sweeper() -> None:
    """后台定时清理过期任务（内存表 + 磁盘文件）。"""
    while True:
        time.sleep(600)
        try:
            jobs.sweep()
        except Exception:  # noqa: BLE001
            log.exception("后台任务清理失败")


jobs = JobStore(JOB_TTL_SECONDS, JOB_MAX_ENTRIES, on_evict=_cleanup_job_files)


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.\-]+$")


def _safe_job_id(raw: str) -> str:
    """job_id 只允许十六进制，杜绝路径穿越。"""
    if not raw or not re.fullmatch(r"[0-9a-f]{6,32}", raw):
        raise HTTPException(status_code=400, detail="任务 ID 非法")
    return raw


def _resolve_upload(filename: str) -> str:
    """把用户提供的文件名安全地映射到 UPLOAD_DIR 下的真实路径。"""
    base = os.path.basename(filename)
    if not base or not _SAFE_NAME.match(base):
        raise HTTPException(status_code=400, detail="文件名非法")
    path = os.path.realpath(os.path.join(UPLOAD_DIR, base))
    root = os.path.realpath(UPLOAD_DIR)
    if os.path.commonpath([path, root]) != root:
        raise HTTPException(status_code=400, detail="文件名非法")
    return path


def _validate_image(path: str) -> None:
    """确认落盘的是真图片，并检查像素规模。"""
    import cv2  # 延迟导入，加快启动

    if os.path.getsize(path) == 0:
        raise HTTPException(status_code=400, detail="上传文件为空")
    probe = cv2.imread(path, cv2.IMREAD_REDUCED_COLOR_8)
    if probe is None:
        raise HTTPException(status_code=400, detail="无法识别的图片格式")
    h, w = probe.shape[:2]
    # 缩略图是 1/8 尺寸，换算回原图
    if h * 8 * w * 8 > MAX_PIXELS:
        raise HTTPException(
            status_code=413,
            detail="图片像素过大（上限 %d 万像素），请先缩小。" % (MAX_PIXELS // 10_000),
        )


def _normalize_long_side(src: str, dst: str, target: int = 0,
                         aspect_ratio: str = "") -> str:
    """把长边缩放到 target，支持指定画幅比例居中裁剪，输出 JPEG q95。

    这一步对齐了原站的做法（我们逐字节复现过：sharp/libvips 的 lanczos3）。
    统一尺寸有两个好处：模型输入稳定，成本可预测。

    输出 JPEG 而不是 PNG：1536 长边的照片存 PNG 要 3~5 MB，转成 data URI
    上传给模型时还要再涨三分之一；q95 的 JPEG 只有几百 KB，肉眼看不出差别。
    target<=0 时只做一次转码，不改尺寸。target 从后台设置现读。
    """
    import cv2

    img = cv2.imread(src, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("无法读取图片: %s" % src)

    if aspect_ratio:
        img = _crop_aspect_ratio(img, aspect_ratio)

    h, w = img.shape[:2]
    long_side = max(h, w)
    if target > 0 and long_side != target:
        scale = target / float(long_side)
        new_w, new_h = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
        # 缩小用 INTER_AREA，放大用 INTER_LANCZOS4（libvips lanczos3 的等价物）
        interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LANCZOS4
        img = cv2.resize(img, (new_w, new_h), interpolation=interp)
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
    if not ok:
        raise ValueError("图片编码失败")
    buf.tofile(dst)
    return dst


def _media_type(path: str) -> str:
    return mimetypes.guess_type(path)[0] or "application/octet-stream"


# --------------------------------------------------------------------------- #
# 处理管线
# --------------------------------------------------------------------------- #
def _run_pipeline(job_id: str, quality: str, style: str,
                  template: Optional[Dict[str, Any]] = None,
                  text_values: Optional[Dict[str, str]] = None,
                  aspect_ratio: str = "") -> None:
    """后台执行：归一化 -> 按 settings.chain 依次尝试 -> 本地兜底
    -> 模板输出尺寸 -> 模板文字排版。

    链路顺序、供应商开关、密钥、提示词全部来自运行时设置（/admin 可热改），
    每一档任务执行时现读；模板参数用提交那一刻的快照；
    某一级失败自动落到下一级，本地引擎永远兜底 ——
    就算它被关掉，全链失败时也会强制跑一次。
    """
    job = jobs.get(job_id)
    if job is None:
        return

    src = os.path.join(UPLOAD_DIR, job["orig_file"])
    out = os.path.join(UPLOAD_DIR, job["result_file"])
    # 归一化与中间结果都用 JPEG：体积小、上传快，肉眼无差别
    norm = os.path.join(UPLOAD_DIR, "norm_%s.jpg" % job_id)
    tmp = os.path.join(UPLOAD_DIR, "tmp_%s.jpg" % job_id)

    stage = "normalize"
    provider_used = None
    template = template or {}
    try:
        # --- 1. 归一化（包含画幅裁剪）---
        _normalize_long_side(src, norm, target=settings.normalize_long_side(),
                             aspect_ratio=aspect_ratio)
        jobs.update(job_id, stage="enhance")

        # 归一化图上传 COS 并生成签名直链：让网关自己来拉，
        # 服务器→供应商的公网出方向流量归零（同地域内网上传免费）
        gateway_url = None
        if settings.cos_ready():
            try:
                norm_key = "norms/%s/%s.jpg" % ((job.get("openid") or "anon")[:8],
                                                job_id)
                with open(norm, "rb") as fh:
                    cos_put(settings, norm_key, fh.read())
                gateway_url = cos_presign(settings, "get", norm_key,
                                          ttl_seconds=3600)
                jobs.update(job_id, norm_cos=norm_key)
                log.info("[%s] URL 直连就绪：%s", job_id, norm_key)
            except CosError as exc:
                log.warning("[%s] 归一化图传 COS 失败，回退 multipart: %s",
                            job_id, exc)

        # --- 2. 按链路逐级尝试 ---
        stage = "enhance"
        enhanced = False
        # 模板提示词优先；无模板按档位取全局提示词
        tier_prompt = str(template.get("prompt") or "").strip() \
            or settings.prompt_for(quality)

        for name in settings.chain():
            if enhanced:
                break
            if name == "local":
                continue  # 本地引擎是最后兜底，见链尾
            if not settings.provider_enabled(name):
                log.info("[%s] %s 已在后台停用，跳过", job_id, name)
                continue
            client = _get_client(name)
            if client is None or not client.configured:
                log.info("[%s] %s 未配置，跳过", job_id, name)
                continue
            try:
                if name == "worldcodes":
                    # 网关是提示词驱动的编辑模型，提示词从后台设置现读；
                    # 模板可覆盖模型（原生 2K/4K）与尺寸参数；
                    # 有 COS 直链时走 URL 直连（网关自己拉图），失败自动回退 multipart
                    client.enhance(norm, tmp, quality=quality, style=style,
                                   prompt=tier_prompt,
                                   size=str(template.get("gateway_size") or ""),
                                   model=str(template.get("model_override") or ""),
                                   image_url=gateway_url)
                else:
                    client.enhance(norm, tmp, quality=quality, style=style)
                enhanced = True
                provider_used = name
                cost_cny = getattr(client, "last_cost_cny", None)
                cost_usd = getattr(client, "last_cost_usd", None)
                scale = getattr(client, "last_scale", None)
                jobs.update(job_id, provider=name, cost_cny=cost_cny,
                            cost_usd=cost_usd, scale=scale)
                notice = getattr(client, "last_notice", None)
                if notice:
                    log.warning("[%s] %s", job_id, notice)
                log.info(
                    "[%s] %s 完成（%s 档，模板=%s，成本 ¥%s / $%s）",
                    job_id, name, quality, template.get("name", "-"),
                    ("%.3f" % cost_cny) if cost_cny is not None else "-",
                    ("%.4f" % cost_usd) if cost_usd is not None else "-",
                )
            except Exception as exc:  # noqa: BLE001
                code = getattr(exc, "code", exc.__class__.__name__)
                log.warning("[%s] %s 失败(%s)：%s", job_id, name, code, exc)

        # --- 3. 本地兜底：链路全挂（或全部被停用）时强制跑一次 ---
        if not enhanced:
            log.info("[%s] 外部链路全部失败，走本地引擎（quality=%s, style=%s）",
                     job_id, quality, style)
            # 前面已经归一化过，这里关掉 2K 上采样，避免把 1536 插值回 2000
            engine.process(norm, tmp, quality=quality, upscale_2k=False,
                           style=style)
            provider_used = "local"

        stage = "finalize"
        # --- 4. 模板输出尺寸：印刷级模板在这里放大到目标长边 ---
        output_size = int(template.get("output_size") or 0)
        if output_size > 0:
            _resize_long_side(tmp, tmp, target=output_size)

        # --- 5. 模板文字排版：底图 + 代码叠加中文（字保真，模型不碰字） ---
        spec = template.get("text_fields") or []
        if spec:
            merged = collect_values(spec, text_values or {})
            if merged:
                layout = str(template.get("layout") or "postcard_bottom")
                apply_text_overlay(tmp, tmp, layout, spec, merged)
                log.info("[%s] 文字排版完成（%s，%d 项）",
                         job_id, layout, len(merged))

        # --- 6. 输出 ---
        _finalize(tmp, out)
        # 结果图也要过一道审核：AI 输出可能触发边界内容
        with open(out, "rb") as fh:
            reject = _moderate_or_reject(fh.read(), job_id, job.get("openid", ""))
        if reject:
            try:
                os.remove(out)
            except OSError:
                pass
            jobs.update(job_id, status="failed", stage=stage, error=reject)
            _refund_charged(job.get("openid", ""), job_id,
                            int(job.get("price") or 0))
            log.warning("[%s] 结果被审核拦截: %s", job_id, reject)
            return
        # 结果图上传 COS（登记时预留的 result_cos 键），用户下载走 COS 直链
        if job.get("result_cos"):
            try:
                with open(out, "rb") as fh:
                    cos_put(settings, job["result_cos"], fh.read())
            except CosError as exc:
                log.warning("[%s] 结果传 COS 失败，退本地直链: %s", job_id, exc)
                jobs.update(job_id, result_cos=None)
        jobs.update(job_id, status="succeeded", stage="done",
                    provider=provider_used)
        users.audit(job.get("openid", ""), "completed", "job=%s" % job_id)
        log.info("[%s] 任务完成 -> %s", job_id, os.path.basename(out))

    except Exception as exc:  # noqa: BLE001
        log.exception("[%s] 处理失败（stage=%s）", job_id, stage)
        jobs.update(job_id, status="failed", error=str(exc)[:500], stage=stage)
        _refund_charged(job.get("openid", ""), job_id,
                        int(job.get("price") or 0))
    finally:
        for path in (norm, tmp):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass


def _finalize(src: str, dst: str) -> None:
    """把结果统一成 JPEG 输出。

    已经由上游编码好的 JPEG 直接搬，不重编码 —— 二次编码只会白白掉画质。
    只有 PNG / WebP 这类才重新编码一次。
    """
    import cv2

    if not os.path.exists(src) or os.path.getsize(src) == 0:
        raise RuntimeError("增强结果为空")

    with open(src, "rb") as fh:
        magic = fh.read(3)
    if magic == b"\xff\xd8\xff":          # 已经是 JPEG
        shutil.copyfile(src, dst)
        return

    img = cv2.imread(src, cv2.IMREAD_COLOR)
    if img is None:
        # 某些编码 cv2 读不了，直接搬过去，至少不让任务失败
        shutil.copyfile(src, dst)
        return
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
    if not ok:
        raise RuntimeError("结果编码失败")
    buf.tofile(dst)


def _resize_long_side(path: str, dst: str, target: int) -> None:
    """把图片长边统一到 target（印刷级模板用）。

    网关已按 size 参数原生输出时这里基本是直通；网关不支持 size 时，
    这里用 Lanczos 补到目标长边（插值放大，打印够格，但不如原生）。
    """
    import cv2

    img = cv2.imread(path, cv2.IMREAD_COLOR)
    if img is None:
        return  # 读不了就不动它，交给 _finalize 兜底
    h, w = img.shape[:2]
    long_side = max(h, w)
    if long_side == target or long_side <= 0:
        return
    scale = target / float(long_side)
    new_w, new_h = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LANCZOS4
    img = cv2.resize(img, (new_w, new_h), interpolation=interp)
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
    if ok:
        buf.tofile(dst)


# --------------------------------------------------------------------------- #
# 路由
# --------------------------------------------------------------------------- #
# 风格表、档位默认风格、鱼干价格全部迁到运行时设置（/admin 可改），
# /api/styles 与 /api/config 现读。


def _default_style_for(quality: str) -> str:
    return settings.quality_to_style().get(quality, "gym_contrast")


def _admin_stats() -> Dict[str, Any]:
    """后台仪表盘统计：按本地自然日聚合。"""
    now = time.time()
    lt = time.localtime(now)
    midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                            0, 0, 0, 0, 0, -1))
    items, total = jobs.list_recent(0, 10 ** 6)
    today = [j for j in items if j.get("created_at", 0) >= midnight]
    out = {
        "today_total": len(today),
        "today_succeeded": sum(1 for j in today if j.get("status") == "succeeded"),
        "today_failed": sum(1 for j in today if j.get("status") == "failed"),
        "running": sum(1 for j in items if j.get("status") == "processing"),
        "today_cost_cny": sum(float(j.get("cost_cny") or 0) for j in today),
        "today_cost_usd": sum(float(j.get("cost_usd") or 0) for j in today),
        "total": total,
    }
    try:
        out.update(users.stats())
    except Exception:  # noqa: BLE001
        pass
    return out


@app.get("/api/health")
def health() -> Dict[str, Any]:
    """健康检查。configured=false 说明所有 AI 后端都没配，只剩本地引擎。"""
    wc = settings.provider("worldcodes")
    gateway_ok = bool(wc.get("base_url") and wc.get("api_key"))
    fal_conf = bool(settings.provider("fal").get("api_key")) \
        or bool(os.environ.get("FAL_KEY", "").strip())
    bd = settings.provider("baidu")
    baidu_conf = bool(bd.get("api_key") and bd.get("secret_key"))
    cos_info = None
    if settings.cos_ready():
        try:
            from cos_store import check_internal
            cos_info = check_internal(settings)
        except Exception as e:
            cos_info = {"ok": False, "error": str(e)}
    mod = settings.moderation()
    wx_ready = wechat_sec.wechat_sec_ready(settings)
    tc_ready = settings.moderation_ready()
    return {
        "ok": True,
        "gateway": gateway_ok,
        "fal": fal_conf,
        "baidu": baidu_conf,
        "local": True,
        "configured": gateway_ok or fal_conf or baidu_conf,
        "cos_network": cos_info,
        "moderation": {
            "enabled": bool(mod.get("enabled")),
            "block_on_error": bool(mod.get("block_on_error")),
            "wechat_sec_ready": wx_ready,
            "tencent_ims_ready": tc_ready,
            "active_engine": "wechat_free" if wx_ready else ("tencent_ims" if tc_ready else "none"),
        },
        "chain": settings.chain(),
        "maintenance": settings.maintenance().get("enabled", False),
        "normalize_long_side": settings.normalize_long_side(),
        "max_upload_bytes": MAX_UPLOAD_BYTES,
    }


@app.get("/api/styles")
def get_available_styles() -> Dict[str, Any]:
    return {"styles": settings.styles()}


@app.get("/api/config")
def public_config() -> Dict[str, Any]:
    """小程序启动时拉取：价格、维护状态、风格表、调试免扣费开关。"""
    return {
        "prices": settings.prices(),
        "free_mode": settings.free_mode(),
        "cos_ready": settings.cos_ready(),
        "maintenance": settings.maintenance(),
        "styles": settings.styles(),
    }


@app.get("/api/announcements")
def public_announcements() -> Dict[str, Any]:
    """小程序首页公告横幅数据源（只返回启用的，最新在前）。"""
    return {"items": announcements.list_enabled(limit=5)}


@app.get("/api/templates")
def public_templates() -> Dict[str, Any]:
    """模板商店数据源：分组 + 启用的模板（现读，后台改完即生效）。

    封面 URL 已解析好：COS 签名直链 / 本地 / 外链，
    图片流量不走后端带宽；提示词不下发。
    """
    return {
        "groups": templates.list_groups(enabled_only=True),
        "items": templates.public_templates(settings),
    }


@app.get("/api/covers/{filename}")
def get_cover(filename: str) -> FileResponse:
    """模板封面本地服务（开发期 / 未配 COS 时用；配了 COS 走签名直链）。"""
    base = os.path.basename(filename)
    if not base or not _SAFE_NAME.match(base):
        raise HTTPException(status_code=400, detail="文件名非法")
    path = os.path.realpath(os.path.join(covers_dir(), base))
    root = os.path.realpath(covers_dir())
    if os.path.commonpath([path, root]) != root or not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="封面不存在")
    return FileResponse(
        path,
        media_type=_media_type(path),
        headers={"Cache-Control": "public, max-age=604800"},
    )


# 微信消息推送（明文模式 + JSON）的请求体，字段不固定，收下整个对象
_WxPushBody = Dict[str, Any]


def _wxpush_signature_ok(request: Request) -> bool:
    token = str(settings.moderation().get("wechat_push_token") or "").strip()
    if not token:
        return False  # 未配置推送 Token：无法校验，视为不通过
    q = request.query_params
    sig = q.get("signature") or q.get("msg_signature") or ""
    return wechat_sec.verify_push_signature(
        token, sig, q.get("timestamp", ""), q.get("nonce", ""))


@app.get("/api/wxpush")
@app.get("/wxpush")
def wxpush_verify(request: Request):
    """微信公众平台「消息推送」的 URL 校验：原样返回 echostr。"""
    if not _wxpush_signature_ok(request):
        raise HTTPException(status_code=403, detail="签名校验失败")
    return PlainTextResponse(request.query_params.get("echostr") or "")


@app.post("/api/wxpush")
@app.post("/wxpush")
def wxpush_message(body: _WxPushBody, request: Request):
    """微信内容安全 mediaCheckAsync 的异步结果推送端点。"""
    if not _wxpush_signature_ok(request):
        raise HTTPException(status_code=403, detail="签名校验失败")
    parsed = wechat_sec.parse_push_body(body or {})
    if parsed is None:
        log.info("收到非审核类微信推送或安全模式包体，忽略: %s",
                 str(body)[:150])
    else:
        trace_id, suggest, label, score = parsed
        hit = wechat_sec.resolve_pending(trace_id, suggest, label, score)
        log.info("微信审核推送: trace=%s suggest=%s label=%s 命中=%s",
                 trace_id, suggest, label, hit)
    return PlainTextResponse("success")


@app.get("/")
def home_page(request: Request):
    """网页端画质修复控制台页面；若微信发送消息推送校验（带 echostr），直接兼容响应。"""
    q = request.query_params
    if q.get("echostr") and (q.get("signature") or q.get("msg_signature")):
        return wxpush_verify(request)
    return FileResponse(
        os.path.join(BASE_DIR, "index.html"),
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


@app.post("/")
def home_post(body: _WxPushBody, request: Request):
    """兼容微信推送 URL 填成根路径的情况。"""
    return wxpush_message(body, request)


@app.get("/logo.jpg")
def app_logo() -> FileResponse:
    """应用图标与头像。"""
    return FileResponse(
        os.path.join(BASE_DIR, "logo.jpg"),
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/admin")
def admin_page() -> FileResponse:
    """管理后台页面本体。API 在 /admin/api/*，密码见 .env 的 ADMIN_PASSWORD。"""
    return FileResponse(
        os.path.join(BASE_DIR, "admin.html"),
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


app.include_router(make_admin_router(
    settings=settings,
    announcements=announcements,
    templates=templates,
    jobs=jobs,
    users=users,
    health_fn=health,
    stats_fn=_admin_stats,
))


def _rescue_guard(request) -> Dict[str, Any]:
    """所有提交路径共用的前置检查：登录 -> 维护 -> 封禁 -> 频控/配额。返回用户行。"""
    user = _current_user(request)
    if user.get("banned"):
        users.audit(user["openid"], "banned_submit_attempt")
        raise HTTPException(status_code=403, detail="账号已被封禁，如有疑问请联系客服")
    mt = settings.maintenance()
    if mt.get("enabled"):
        raise HTTPException(
            status_code=503,
            detail=mt.get("message") or "服务维护中，请稍后再试")
    reason = _check_quota(user["openid"])
    if reason:
        raise HTTPException(status_code=429, detail=reason)
    return user


def _validate_quality_style(quality: str, style: str) -> Tuple[str, str]:
    quality = (quality or "fine").strip().lower()
    if quality not in ("light", "fine"):
        raise HTTPException(status_code=400, detail="quality 只能是 light 或 fine")
    style_ids = {s["id"] for s in settings.styles() if s["id"] != "ai"}
    style = (style or "").strip() or _default_style_for(quality)
    if style == "ai" or style not in style_ids:
        style = _default_style_for(quality)
    return quality, style


def _resolve_template(template_id: str, text_fields_raw: str = ""
                      ) -> Tuple[Optional[Dict[str, Any]], str, Dict[str, str]]:
    """解析模板提交参数。

    返回 (模板快照或 None, 引擎档位, 文字字段值)。
    template_id 提供但找不到/已停用 -> 400（客户端拿到的是旧列表时保护）；
    模板存在时引擎档位以模板 engine 为准（quality 参数仅纯修复模式生效）；
    模板文字字段做长度截断 + 默认值补齐（collect_values）。
    """
    template_id = (template_id or "").strip()
    if not template_id:
        return None, "", {}
    tpl = templates.get_template(template_id, enabled_only=True)
    if tpl is None:
        raise HTTPException(status_code=400,
                            detail="模板不存在或已下架，刷新后重试")
    # 引擎档位跟模板走（quality 参数在模板模式下仅作兜底）
    quality = str(tpl.get("engine") or "fine")

    values: Dict[str, str] = {}
    raw = (text_fields_raw or "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            values = {str(k): str(v) for k, v in parsed.items()
                      if isinstance(k, str)}
    spec = tpl.get("text_fields") or []
    merged = collect_values(spec, values)
    return tpl, quality, merged


def _safe_remove(path: str) -> bool:
    try:
        if os.path.exists(path):
            os.remove(path)
            return True
    except OSError:
        pass
    return False


class _LoginBody(BaseModel):
    code: str = ""


class _EarnBody(BaseModel):
    kind: str = ""


class _InviteBody(BaseModel):
    code: str = ""


class _RescueByUploadBody(BaseModel):
    upload_id: str = ""
    quality: str = ""
    style: str = ""
    template_id: str = ""
    text_fields: str = ""
    aspect_ratio: str = ""


def _login_response(openid: str) -> Dict[str, Any]:
    user = users.ensure_user(openid)
    users.audit(openid, "login")
    return {
        "token": user_token(openid),
        "user": {"openid_masked": openid[:6] + "***",
                 "total_jobs": user["total_jobs"]},
        "balance": user["balance"],
        "invite_code": user["invite_code"],
        "free_mode": settings.free_mode(),
        "cos_ready": settings.cos_ready(),
    }


@app.post("/api/auth/login")
def wechat_login(body: _LoginBody):
    """wx.login 的 code 换用户会话 token（12h）。

    同步处理：code2session 是阻塞网络调用，交给 FastAPI 线程池，
    不许堵住事件循环。
    """
    code = (body.code or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="缺少 code")
    try:
        openid = code2session(code, settings)
    except WechatAuthError as exc:
        raise HTTPException(status_code=401 if exc.code != "NOT_CONFIGURED" else 503,
                            detail=str(exc)) from exc
    return _login_response(openid)


@app.post("/api/auth/web")
def web_login(request: Request):
    """网页控制台登录：按来源 IP 派生稳定访客身份，发同样的会话 token。

    网页端做不了 wx.login，用 IP 哈希当 openid 即可接入同一套
    配额 / 光子 / 任务归属体系（同 IP 共享配额与余额）。
    """
    ip = request.client.host if request.client else "unknown"
    openid = "web-" + hashlib.sha256(("web:" + ip).encode("utf-8")).hexdigest()[:12]
    return _login_response(openid)


@app.get("/api/me")
def get_me(request: Request):
    """当前用户资料：光子余额、邀请码、今日奖励进度、封禁状态。"""
    user = _current_user(request)
    openid = user["openid"]
    return {
        "openid_masked": openid[:6] + "***",
        "balance": user["balance"],
        "total_jobs": user["total_jobs"],
        "invite_code": user["invite_code"],
        "banned": user.get("banned", False),
        "earn": {
            "checkin_done": users.earn_count_today(openid, "checkin") > 0,
            "video_today": users.earn_count_today(openid, "video"),
        },
    }


@app.post("/api/me/earn")
def earn_points(body: _EarnBody, request: Request):
    """每日奖励：checkin（1 次/天）与 video（3 次/天）。

    次数校验与发放同事务落 SQLite，清缓存 / 改本地数据都刷不掉。
    """
    user = _current_user(request)
    kind = (body.kind or "").strip()
    conf = EARN_DEFS.get(kind)
    if conf is None:
        raise HTTPException(status_code=400, detail="未知的奖励类型")
    ok, balance, count = users.earn(user["openid"], kind,
                                    conf["reward"], conf["limit"])
    if not ok:
        raise HTTPException(status_code=429,
                            detail="今日%s奖励已领完，明天再来吧" % conf["label"])
    return {"ok": True, "kind": kind, "reward": conf["reward"],
            "balance": balance, "count_today": count}


@app.post("/api/me/invite")
def bind_invite(body: _InviteBody, request: Request):
    """绑定邀请码：邀请人与被邀请人各得奖励，每人只能被邀请一次。"""
    user = _current_user(request)
    openid = user["openid"]
    code = (body.code or "").strip().upper()
    if not re.fullmatch(r"INV-[0-9A-F]{8}", code):
        raise HTTPException(status_code=400, detail="邀请码格式不对")
    inviter = users.get_by_invite_code(code)
    if inviter is None:
        raise HTTPException(status_code=400, detail="邀请码不存在")
    if inviter["openid"] == openid:
        raise HTTPException(status_code=400, detail="不能填写自己的邀请码")
    if users.action_count(openid, "invite_bound") > 0:
        raise HTTPException(status_code=400, detail="已经绑定过邀请码了")
    inviter_balance = users.add_balance(inviter["openid"], 30)
    my_balance = users.add_balance(openid, 30)
    users.audit(openid, "invite_bound", "by=%s" % inviter["invite_code"])
    users.audit(inviter["openid"], "invite_reward",
                "invitee=%s***" % openid[:6])
    return {"ok": True, "balance": my_balance,
            "inviter_balance": inviter_balance}





@app.post("/api/uploads")
def create_upload(request: Request,
                  filename: str = Form("photo.jpg"),
                  byte_size: int = Form(0)):
    """申请 COS 预签名直传地址。未配置 COS 时返回 503。"""
    user = _current_user(request)
    if not settings.cos_ready():
        raise HTTPException(status_code=503, detail="对象存储未配置")
    ext = os.path.splitext(filename or "")[1].lower() or ".jpg"
    if ext not in ALLOWED_EXT:
        raise HTTPException(status_code=400,
                            detail="仅支持 JPG / PNG / WebP / BMP / TIFF")
    if byte_size > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="图片不能超过 %d MB"
                            % (MAX_UPLOAD_BYTES // 1024 // 1024))
    upload_id = uuid.uuid4().hex[:16]
    key = "uploads/%s/%s%s" % (user["openid"][:8], upload_id, ext)
    url = cos_presign(settings, "put", key, ttl_seconds=3600)
    with _uploads_lock:
        _sweep_uploads_locked()
        _uploads[upload_id] = {"openid": user["openid"], "key": key,
                               "filename": filename, "ext": ext,
                               "created_at": time.time()}
    return {"upload_id": upload_id, "url": url, "key": key}


@app.post("/api/uploads/{upload_id}/complete")
def complete_upload(upload_id: str, request: Request):
    """确认直传已完成（COS HEAD 验证对象确实存在）。"""
    user = _current_user(request)
    if not settings.cos_ready():
        raise HTTPException(status_code=503, detail="对象存储未配置")
    with _uploads_lock:
        rec = _uploads.get(upload_id)
    if rec is None or rec["openid"] != user["openid"]:
        raise HTTPException(status_code=404, detail="上传登记不存在")
    if not cos_head(settings, rec["key"]):
        raise HTTPException(status_code=400,
                            detail="COS 上还没有这个文件，先完成直传")
    return {"ok": True, "key": rec["key"]}


def _chosen_quality(req_q: str, tpl_quality: str) -> str:
    """引擎档位：模板模式跟模板走（与 _resolve_template 的文档口径一致），
    纯修复模式用用户选择的档位，缺省 fine。"""
    if tpl_quality:
        return tpl_quality
    if req_q in ("light", "fine"):
        return req_q
    return "fine"


@app.post("/api/rescue")
def create_rescue_job(
    request: Request,
    image: UploadFile = File(...),
    quality: str = Form("fine"),
    style: str = Form(""),
    template_id: str = Form(""),
    text_fields: str = Form(""),
    aspect_ratio: str = Form(""),
):
    """提交修图任务（multipart 路径；启用 COS 直传后小程序走 /by-upload）。

    支持画幅比例 aspect_ratio（1:1, 3:4, 4:3, 9:16, 16:9, original）。
    template_id 非空时引擎档位/提示词/输出尺寸/文字排版全部以模板为准；
    纯修复模式用用户显式选择的 quality（light/fine）。

    同步处理：cv2 解码、内容审核、COS 上传全是阻塞 IO，
    FastAPI 会把整个 handler 丢进线程池，事件循环不被拖住。
    """
    user = _rescue_guard(request)
    tpl, tpl_quality, text_values = _resolve_template(template_id, text_fields)
    quality, style = _validate_quality_style(
        _chosen_quality((quality or "").strip().lower(), tpl_quality), style)

    ext = os.path.splitext(image.filename or "")[1].lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(
            status_code=400,
            detail="仅支持 JPG / PNG / WebP / BMP / TIFF")

    job_tmp = os.path.join(UPLOAD_DIR,
                           "incoming_%s%s" % (uuid.uuid4().hex[:8], ext))
    try:
        written = 0
        with open(job_tmp, "wb") as fh:
            while True:
                chunk = image.file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail="图片不能超过 %d MB" % (MAX_UPLOAD_BYTES // 1024 // 1024))
                fh.write(chunk)
    except HTTPException:
        _safe_remove(job_tmp)
        raise
    except Exception as exc:  # noqa: BLE001
        _safe_remove(job_tmp)
        raise HTTPException(status_code=500, detail="保存上传文件失败") from exc
    finally:
        try:
            image.file.close()
        except Exception:  # noqa: BLE001
            pass

    try:
        _validate_image(job_tmp)
    except HTTPException:
        _safe_remove(job_tmp)
        raise

    # 上传侧内容审核：AI 调用之前拦，省钱也合规
    with open(job_tmp, "rb") as fh:
        reject = _moderate_or_reject(fh.read(), "upload", user["openid"])
    if reject:
        _safe_remove(job_tmp)
        raise HTTPException(status_code=400, detail=reject)

    try:
        return _register_job(user["openid"], quality, style, job_tmp, ext,
                             template=tpl, text_values=text_values,
                             aspect_ratio=aspect_ratio)
    except Exception:
        _safe_remove(job_tmp)  # 含 402 光子不足等失败路径，别留下孤儿临时文件
        raise


@app.post("/api/rescue/by-upload")
def create_rescue_job_by_upload(payload: _RescueByUploadBody,
                                request: Request):
    """COS 直传路径：JSON {upload_id, quality, style, template_id, text_fields, aspect_ratio}，
    图片字节不过本服务器（从 COS 拉回到本地归一化，出方向流量为零）。"""
    user = _rescue_guard(request)
    upload_id = (payload.upload_id or "").strip()
    tpl, tpl_quality, text_values = _resolve_template(
        payload.template_id, payload.text_fields)
    quality, style = _validate_quality_style(
        _chosen_quality((payload.quality or "").strip().lower(), tpl_quality),
        payload.style)

    with _uploads_lock:
        rec = _uploads.pop(upload_id, None)  # 登记用后即焚，同一 upload_id 不能重复提交
    if rec is None or rec["openid"] != user["openid"]:
        raise HTTPException(status_code=404, detail="上传登记不存在")
    if not cos_head(settings, rec["key"]):
        raise HTTPException(status_code=400, detail="COS 上没有这个文件")

    ext = rec["ext"]
    job_tmp = os.path.join(UPLOAD_DIR,
                           "incoming_%s%s" % (uuid.uuid4().hex[:8], ext))
    try:
        data = cos_get(settings, rec["key"])
        with open(job_tmp, "wb") as fh:
            fh.write(data)
        _validate_image(job_tmp)
    except CosError as exc:
        _safe_remove(job_tmp)
        raise HTTPException(status_code=502, detail="对象存储读取失败") from exc
    except HTTPException:
        _safe_remove(job_tmp)
        raise

    with open(job_tmp, "rb") as fh:
        reject = _moderate_or_reject(fh.read(), "upload", user["openid"])
    if reject:
        _safe_remove(job_tmp)
        raise HTTPException(status_code=400, detail=reject)

    try:
        return _register_job(user["openid"], quality, style, job_tmp, ext,
                             template=tpl, text_values=text_values,
                             aspect_ratio=payload.aspect_ratio)
    except Exception:
        _safe_remove(job_tmp)
        raise


@app.get("/api/jobs/{job_id}")
def query_job_status(job_id: str, request: Request):
    """前端轮询接口。任务只对提交者本人可见（他人查询一律 404，不泄露存在性）。"""
    user = _current_user(request)
    job_id = _safe_job_id(job_id)
    job = jobs.get(job_id)
    if job is None or job.get("openid") != user["openid"]:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    def _media_url(kind: str, fallback: Optional[str]) -> Optional[str]:
        """COS 可用时给签名直链（免服务器带宽），否则本地路径。"""
        key = job.get("%s_cos" % kind)
        if key and settings.cos_ready():
            try:
                return cos_presign(settings, "get", key, ttl_seconds=7200)
            except CosError:
                pass
        return fallback

    return {
        "id": job["id"],
        "status": job["status"],
        "stage": job.get("stage"),
        "quality": job.get("quality"),
        "aspect_ratio": job.get("aspect_ratio", ""),
        "template_id": job.get("template_id", ""),
        "template_name": job.get("template_name", ""),
        "price": job.get("price"),
        "provider": job.get("provider"),
        "error": job.get("error"),
        "orig_url": _media_url("orig", job.get("orig_url")),
        "result_url": _media_url("result", job.get("result_url")),
        "created_at": job.get("created_at"),
    }


@app.get("/api/my/jobs")
def get_my_jobs(request: Request, limit: int = 30):
    """查询当前登录用户最近提交的任务历史列表（支持跨端同步与切屏恢复）。"""
    user = _current_user(request)
    raw_list = jobs.list_for_openid(user["openid"], limit=limit)
    res = []
    for job in raw_list:
        def _media_url(kind: str, fallback: Optional[str]) -> Optional[str]:
            key = job.get("%s_cos" % kind)
            if key and settings.cos_ready():
                try:
                    return cos_presign(settings, "get", key, ttl_seconds=7200)
                except CosError:
                    pass
            return fallback

        res.append({
            "id": job["id"],
            "status": job["status"],
            "stage": job.get("stage"),
            "quality": job.get("quality"),
            "aspect_ratio": job.get("aspect_ratio", ""),
            "template_id": job.get("template_id", ""),
            "template_name": job.get("template_name", ""),
            "error": job.get("error"),
            "orig_url": _media_url("orig", job.get("orig_url")),
            "result_url": _media_url("result", job.get("result_url")),
            "created_at": job.get("created_at"),
        })
    return {"jobs": res}


@app.get("/api/images/{filename}")
def get_image(filename: str) -> FileResponse:
    """静态图片访问。文件名经过白名单校验，防路径穿越。"""
    path = _resolve_upload(filename)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="图片不存在")
    return FileResponse(
        path,
        media_type=_media_type(path),
        headers={"Cache-Control": "private, max-age=86400"},
    )


@app.exception_handler(Exception)
async def unhandled(request, exc):  # noqa: ANN001, ARG001
    log.exception("未处理异常: %s", exc)
    return JSONResponse(status_code=500, content={"detail": "服务器内部错误"})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        reload=bool(os.environ.get("RELOAD")),
    )
