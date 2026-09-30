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
import hmac
import glob
import json
import logging
import mimetypes
import os
import sys
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from functools import partial
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from admin_api import ensure_admin_password, make_admin_router
from cleanup_store import CleanupStore
from community_store import CommunityStore
from template_output import select_template_output, select_template_quality
from text_generation import make_text_router, ready as text_generation_ready
from cloud_pipeline import CloudPipeline
from image_processing import IMAGE_LOCK, image_limited, upload_limited, normalization_rule, BoundedExecutor, QueueFull
from cos_store import process_image as cos_process_image
from credit_packages import GENERATION_COST, POINTS_PER_YUAN, public_packages
from cos_store import (CosError, get_object as cos_get,
                       head_exists as cos_head, presign as cos_presign,
                       put_object as cos_put)
from engine import ImageRescueEngine
from gateway_ai import OpenAIImagesEnhance
from settings_store import AnnouncementStore, SettingsStore
from templates_store import TemplateStore, covers_dir
from text_overlay import apply as apply_text_overlay, collect_values
from tencent_cs import ModerationError, moderate_image_bytes, moderate_text
from user_store import AdmissionError, UserStore
from reward_verifier import verify_video
import wechat_sec
from wechat_sec import WechatSecError
from wechat_auth import (WechatAuthError, bearer_of, code2session,
                         make_token as user_token, verify_token as verify_user_token,
                         make_web_identity, verify_web_identity, WEB_IDENTITY_TTL)
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


def _source_revision() -> str:
    """Bind the running process to its checkout at startup, not to later file changes."""
    root = os.path.realpath(os.path.join(BASE_DIR, ".."))
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=root,
                             capture_output=True, text=True, timeout=3, check=True).stdout.strip()
        if os.path.normcase(os.path.realpath(top)) != os.path.normcase(root):
            return ""  # isolated tests and copied deployments are not this checkout
        revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                                  capture_output=True, text=True, timeout=3, check=True).stdout.strip()
        return revision if re.fullmatch(r"[0-9a-f]{40}", revision) else ""
    except (OSError, subprocess.SubprocessError):
        return ""


SOURCE_REVISION = _source_revision()


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
ORIGINAL_TTL_SECONDS = int(os.environ.get("ORIGINAL_TTL_SECONDS", 24 * 3600))
MEDIA_URL_TTL_SECONDS = 600
_sweeper_stop = threading.Event()

# 每日奖励（服务端记账，客户端只是展示）
EARN_DEFS: Dict[str, Dict[str, Any]] = {
    "checkin": {"reward": 10, "limit": 1, "label": "签到"},
    "video": {"reward": 10, "limit": 3, "label": "看视频"},
}

# 归一化长边改由后台设置热调（settings.normalize_long_side()），
# 环境变量 NORMALIZE_LONG_SIDE 仅作为首次初始化的默认值。

# 线程池：cv2 / 网络 IO 都会阻塞，别占住事件循环
WORKERS = int(os.environ.get("WORKERS", 4))
MAX_QUEUED_JOBS = max(0,int(os.environ.get("MAX_QUEUED_JOBS",32)))

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
    _sweeper_stop.clear()
    jobs.sweep()
    _startup_file_gc()
    threading.Thread(target=_bg_sweeper, name="job-sweeper",
                     daemon=True).start()
    cloud.start()
    yield
    _sweeper_stop.set()
    cloud.stop()
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


@app.middleware('http')
async def cloud_upload_gate(request: Request, call_next):
    # Reject the legacy upload before FastAPI parses/spools its multipart body.
    if request.method=='POST' and request.url.path=='/api/rescue' and cloud.enabled():
        return JSONResponse(status_code=503,content={'detail':'云端模式请使用 COS 直传，不经后端上传图片'})
    return await call_next(request)

engine = ImageRescueEngine(lut_dir=LUT_DIR)


# --------------------------------------------------------------------------- #
# 运行时设置 + 公告（backend/data/*.json，后台 /admin 可热改）
# --------------------------------------------------------------------------- #
DATA_DIR = os.environ.get("DATA_DIR") or os.path.join(BASE_DIR, "data")


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
cleanup = CleanupStore(DATA_DIR, UPLOAD_DIR)


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


def _get_client(name: str, quality: str = 'light'):
    """按当前设置取客户端；配置指纹变化时重建。返回 None 表示不可用。

    构建动作很轻（只是 new 一个 HTTP 客户端对象），直接在锁内完成，
    避免两个线程同时构建互相覆盖。
    """
    conf = settings.gateway_for(quality) if name=='worldcodes' else settings.provider(name)
    fp = _fingerprint(conf)
    with _clients_lock:
        cache_key = (name, quality if name=='worldcodes' else '', threading.get_ident())
        cached = _clients.get(cache_key)
        if cached is not None and cached[0] == fp:
            return cached[1]
        client = _build_client(name, conf)
        _clients[cache_key] = (fp, client)
        return client


pool = BoundedExecutor(WORKERS,MAX_QUEUED_JOBS)


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
    user = users.get_user(openid)
    if not user:
        raise HTTPException(status_code=401, detail="账号记录不存在，请重新登录")
    active_app_id = settings.wechat().get("app_id") or ""
    if user.get("account_type") == "wechat" and user.get("app_id") and active_app_id \
            and user["app_id"] != active_app_id:
        raise HTTPException(status_code=401, detail="小程序配置已变更，请重新登录")
    if user.get("admin_hidden") or user["last_seen"] < time.time() - 300:
        users.touch_user(openid)
    return user


# 直传登记：upload_id -> 登记，1 小时未完成自动作废
_uploads: Dict[str, Dict[str, Any]] = {}
_uploads_lock = threading.Lock()
_media_repair_lock = threading.Lock()


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
                settings, image_bytes, openid, cleanup_store=cleanup)
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
        desc = _sec_label_desc(label)
        return "图片内容未通过安全审核（%s）" % desc if desc and desc != "100" else "图片内容未通过安全审核"
    return None


def _moderate_text_or_reject(content: str, openid: str) -> Optional[str]:
    if not content.strip() or not settings.moderation().get("enabled"):
        return None
    verdict = None
    if not openid.startswith("web-") and wechat_sec.wechat_text_ready(settings):
        try:
            verdict = wechat_sec.check_text(settings, content, openid)
        except WechatSecError as exc:
            users.audit(openid, "text_moderation_error", exc.code)
    if verdict is None and settings.moderation_ready():
        try:
            verdict = moderate_text(content, settings)
        except ModerationError as exc:
            users.audit(openid, "text_moderation_error", exc.code)
    if verdict is None:
        raise HTTPException(status_code=503, detail="文字审核服务不可用，请稍后重试")
    suggestion, label, score = verdict
    if suggestion.lower() != "pass":
        return "文字内容未通过安全审核（%s）" % _sec_label_desc(label)
    return None


def _user_text(prompt: str, text_values: Dict[str, str], template: Optional[Dict[str, Any]]) -> Tuple[str, str]:
    custom = str(prompt or "").strip()
    if len(custom) > 500:
        raise HTTPException(status_code=400, detail="修复需求最多 500 字")
    if template and custom:
        raise HTTPException(status_code=400, detail="自定义修复需求仅适用于原片修复")
    combined = "\n".join([custom] + [str(v) for v in text_values.values() if v])
    return custom, combined


def _reject_uploaded_content(openid: str, kind: str, reason: str,
                             quality: str, template: Optional[Dict[str, Any]]) -> None:
    """只对明确的用户输入违规执行扣点；审核服务故障、AI 输出违规均不处罚。"""
    if "审核服务" in reason:
        raise HTTPException(status_code=503, detail=reason)
    price = 0 if settings.free_mode() else _effective_price(quality, template)
    event = users.record_violation(openid, uuid.uuid4().hex[:12], kind, reason, price)
    raise HTTPException(status_code=422, detail={"code": "CONTENT_VIOLATION",
                        "message": reason, **event})


def _refund_charged(openid: str, job_id: str, price: int) -> None:
    """The price argument is display-only; the durable debit determines refund."""
    balance = users.refund_job(openid, job_id)
    log.info("[%s] 扣款流水已核对，余额 %d", job_id, balance)


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
                  aspect_ratio: str = "", custom_prompt: str = "") -> Dict[str, Any]:
    """把已落盘的原图登记为任务（ multipart 与 COS 直传共用这条尾巴）。

    模板提供风格与排版；档位、模型和价格跟随用户选择及全局修图设置。
    光子在提交时预扣（服务端记账，余额不足直接 402），任务失败自动退款。
    """
    # 模板构图由模板/模型决定，旧客户端传来的比例也不能裁掉原始素材。
    if template:
        template=select_template_quality(template,quality)
        aspect_ratio = ""
    if (custom_prompt and not template) or (template or {}).get("requires_prompt"):
        prompt_client = _get_client("worldcodes", quality)
        if not settings.provider_enabled("worldcodes") or prompt_client is None or not prompt_client.configured:
            raise HTTPException(status_code=503, detail="指定成品形式或补充要求的生成引擎当前不可用，请稍后重试")
    price = _effective_price(quality, template)
    job_id = uuid.uuid4().hex[:12]
    free = settings.free_mode()
    charged = 0 if free else price
    try:
        balance = users.reserve_job(openid, job_id, charged, settings.quota(), free)
    except AdmissionError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    orig_path = None
    orig_cos = result_cos = None
    try:
        orig_file = "orig_%s%s" % (job_id, ext)
        orig_path = os.path.join(UPLOAD_DIR, orig_file)
        shutil.move(orig_tmp_path, orig_path)

        # 若指定了非原图画幅比例裁剪，原图同步按相同画幅居中裁剪，
        # 保证原画与重构图画幅、构图与像素级视差 100% 对齐（彻底消除对比滑块错位重影问题）
        ar = str(aspect_ratio).strip().lower()
        if ar and ar not in ("original", "auto", "none", "") and not (settings.cos_ready() and settings.snapshot()['processing']['ci_enabled']):
            try:
                import cv2
                with IMAGE_LOCK:
                    _img = cv2.imread(orig_path, cv2.IMREAD_COLOR)
                    if _img is not None:
                        _cropped = _crop_aspect_ratio(_img, ar)
                        _target_ext = ext if ext.lower() in (".jpg", ".jpeg", ".png") else ".jpg"
                        _ok, _buf = cv2.imencode(_target_ext, _cropped, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
                        if _ok:_buf.tofile(orig_path)
            except Exception as _exc:
                log.warning("[%s] 原图居中裁剪同步失败: %s", job_id, _exc)

        result_file = "result_%s.jpg" % job_id

        orig_cos = result_cos = None
        if settings.cos_ready():
            # 原图存储失败也不应跳过独立的结果上传。
            result_cos = "results/%s/%s.jpg" % (openid[:8], job_id)
            try:
                orig_cos = "origins/%s/%s%s" % (openid[:8], job_id, ext)
                cleanup.schedule("cos", orig_cos, time.time() + ORIGINAL_TTL_SECONDS)
                with open(orig_path, "rb") as fh:
                    cos_put(settings, orig_cos, fh.read())
            except CosError as exc:
                log.warning("[%s] COS 登记失败，回退本地存储: %s", job_id, exc)
                orig_cos = None

        jobs.create(
            job_id,
            openid=openid,
            quality=quality,
            style=style,
            template_id=(template or {}).get("id", ""),
            template_name=(template or {}).get("name", ""),
            template_output_mode=(template or {}).get("output_mode", "template" if template else ""),
            price=price,
            charged_amount=charged,
            aspect_ratio=aspect_ratio,
            orig_file=orig_file,
            result_file=result_file,
            stage="queued",
            orig_url="/api/images/%s" % orig_file,
            result_url="/api/images/%s" % result_file,
            orig_cos=orig_cos,
            result_cos=result_cos,
        )
        users.confirm_job(job_id, "job=%s quality=%s tpl=%s ar=%s price=%d charged=%d"
                          % (job_id, quality, (template or {}).get("id", "-"),
                             aspect_ratio or "-", price, charged))
        cleanup.schedule("local", orig_file, time.time() + ORIGINAL_TTL_SECONDS)
        if orig_cos:
            cleanup.schedule("cos", orig_cos, time.time() + ORIGINAL_TTL_SECONDS)
        pool.submit(_run_pipeline, job_id, quality, style,
                    copy_template(template), dict(text_values or {}), aspect_ratio, custom_prompt)
    except Exception as exc:
        users.refund_job(openid, job_id, cancel=True)
        existing = jobs.get(job_id)
        if existing:
            jobs.update(job_id, status="failed", error="提交任务失败")
            _cleanup_job_files([jobs.get(job_id)])
        elif orig_path:
            _safe_remove(orig_path)
            if orig_cos:
                cleanup.schedule("cos", orig_cos, time.time())
        if isinstance(exc,QueueFull):raise HTTPException(status_code=429,detail=str(exc)) from exc
        raise
    jobs.sweep()
    current = jobs.get(job_id)
    orig_url_val = _job_media_url(current, "orig")

    return {
        "code": 0,
        "job_id": job_id,
        "status": "processing",
        "quality": quality,
        "template_id": (template or {}).get("id", ""),
        "template_name": (template or {}).get("name", ""),
        "template_output_mode": (template or {}).get("output_mode", "template" if template else ""),
        "aspect_ratio": aspect_ratio,
        "price": price,
        "balance": balance,
        "orig_url": orig_url_val,
        "result_url": _job_media_url(current, "result"),
        "free_mode": free,
    }


def _effective_price(quality: str,
                     template: Optional[Dict[str, Any]]) -> int:
    """真实模板与修图同档同价；无模板 ID 的内部计费上下文仍可指定价格。"""
    if (template or {}).get('id'):
        return int(settings.prices().get(quality,0))
    tpl_price = int((template or {}).get("price", 0) or 0)
    if tpl_price > 0:
        return tpl_price
    return int(settings.prices().get(quality, 0))


def copy_template(template: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """任务管线用的是提交那一刻的模板快照（后台随后改配置不影响在途任务）。"""
    return dict(template) if template else None


def _apply_template_output(template, mode):
    try:
        return select_template_output(template, mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# --------------------------------------------------------------------------- #
# 任务存储（进程内，带锁 + TTL 清理）
# --------------------------------------------------------------------------- #
class JobStore:
    """任务表（内存索引 + SQLite 落盘）。

    内存 dict 只作读缓存；每次 create/update 同步写 SQLite，
    所以服务重启后任务记录、作品列表、/api/my/jobs 都还在
    （旧实现是纯内存 dict，重启即清空 —— 这正是"任务记录为空"的原因）。
    单机轻量调度够用；要横向扩容请换成 Redis。
    驱逐（过期/超量）时通过 on_evict 回调通知上层清理磁盘文件。
    """

    _COLUMNS = ("id", "openid", "status", "stage", "quality", "aspect_ratio",
                "template_id", "template_name", "price", "provider",
                "cost_cny", "cost_usd", "scale", "error",
                "orig_file", "result_file", "orig_url", "result_url",
                "orig_cos", "result_cos", "norm_cos",
                "created_at", "updated_at", "charged_amount", "deleted_at",
                "completed_at", "width", "height", "style", "extra_json")

    def __init__(self, ttl: int, max_entries: int,
                 on_evict: Optional[Any] = None,
                 db_path: Optional[str] = None) -> None:
        self._data: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._ttl = ttl
        self._max = max_entries
        self._on_evict = on_evict
        self._startup_evicted: List[Dict[str, Any]] = []
        self._db_path = db_path or os.path.join(BASE_DIR, "data", "jobs.db")
        self._init_db()

    # ---------------------------------------------------------------- #
    # SQLite 落盘
    # ---------------------------------------------------------------- #
    def _init_db(self) -> None:
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS jobs ("
            "  id TEXT PRIMARY KEY,"
            "  openid TEXT, status TEXT, stage TEXT, quality TEXT,"
            "  aspect_ratio TEXT, template_id TEXT, template_name TEXT,"
            "  price INTEGER, provider TEXT, cost_cny REAL, cost_usd REAL,"
            "  scale REAL, error TEXT,"
            "  orig_file TEXT, result_file TEXT, orig_url TEXT,"
            "  result_url TEXT, orig_cos TEXT, result_cos TEXT,"
            "  norm_cos TEXT, created_at REAL, updated_at REAL)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_openid "
                           "ON jobs(openid, created_at DESC)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_created "
                           "ON jobs(created_at DESC)")
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(jobs)")}
        for field, kind in {"charged_amount": "INTEGER", "deleted_at": "REAL",
                            "completed_at": "REAL", "width": "INTEGER", "height": "INTEGER",
                            "style": "TEXT", "extra_json": "TEXT"}.items():
            if field not in columns:
                self._conn.execute("ALTER TABLE jobs ADD COLUMN %s %s" % (field, kind))
        self._conn.commit()
        self._reload()

    def _reload(self) -> None:
        """启动时把未过期的任务读回内存，重启不影响前端轮询。"""
        now = time.time()
        rows = self._conn.execute(
            "SELECT %s FROM jobs" % ", ".join(self._COLUMNS)).fetchall()
        interrupted = []
        all_statuses = {}
        for row in rows:
            job = dict(zip(self._COLUMNS, row))
            raw_extra=job.pop('extra_json',None)
            if raw_extra:
                try:
                    extra=json.loads(raw_extra)
                    if isinstance(extra,dict):job.update({k:v for k,v in extra.items() if k not in self._COLUMNS})
                except (ValueError,TypeError):log.error('任务扩展元数据损坏：%s',job['id'])
            # 重启时仍在 processing 的任务，其工作线程已随旧进程消失：
            # 标记失败并退还预扣光子，避免前端永远转圈
            if job.get("status") == "processing" and not job.get('cloud_pipeline'):
                job["status"] = "failed"
                job["error"] = job.get("error") or "服务重启，任务中断"
                if users.charged_amount(job["id"]) is None:
                    job["error"] += "（历史扣款记录待核对）"
                    users.audit(job.get("openid") or "", "charge_reconciliation_required", "job=" + job["id"])
                interrupted.append(job)
            all_statuses[job["id"]] = "deleted" if job.get("deleted_at") else job["status"]
            if now > self._expires_at(job):
                self._startup_evicted.append(job)
                continue
            self._data[job["id"]] = job
        for job in interrupted:
            # Refund first; re-running after a crash is harmless (ledger unique key).
            users.refund_job(job.get("openid") or "", job["id"])
            self._persist(job)
        self._delete_rows([job["id"] for job in self._startup_evicted])
        users.reconcile_charges(all_statuses)
        if rows:
            log.info("任务表恢复：%d 条记录（在途中断 %d 条，已执行扣款流水恢复）",
                     len(rows), len(interrupted))

    def _persist(self, job: Dict[str, Any]) -> None:
        """整行 upsert。调用方必须持锁。"""
        values = [job.get(col) for col in self._COLUMNS]
        values[-1]=json.dumps({k:v for k,v in job.items() if k not in self._COLUMNS},ensure_ascii=False,separators=(',',':'))
        placeholders = ", ".join("?" * len(self._COLUMNS))
        try:
            self._conn.execute(
                "INSERT OR REPLACE INTO jobs(%s) VALUES(%s)"
                % (", ".join(self._COLUMNS), placeholders), values)
            self._conn.commit()
        except sqlite3.Error:
            self._conn.rollback()
            raise

    def _delete_rows(self, job_ids: List[str]) -> None:
        """驱逐任务时同步删除落盘行。调用方必须持锁。"""
        if not job_ids:
            return
        try:
            self._conn.executemany("DELETE FROM jobs WHERE id = ?",
                                   [(jid,) for jid in job_ids])
            self._conn.commit()
        except sqlite3.Error:
            self._conn.rollback()
            raise

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
            if len(self._data) >= self._max and all(
                    item.get("status") == "processing" for item in self._data.values()):
                raise HTTPException(status_code=503, detail="任务队列已满，请稍后再试")
            self._persist(job)
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
            updated = {**job, **fields, "updated_at": time.time()}
            # A running worker must never resurrect a deleted task.
            if job.get("deleted_at") and fields.get("status") == "succeeded":
                return
            self._persist(updated)
            self._data[job_id] = updated

    def _expires_at(self, job: Dict[str, Any]) -> float:
        return float(job.get("completed_at") or job.get("created_at") or 0) + self._ttl

    def delete_for_openid(self, job_id: str, openid: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            job = self._data.get(job_id)
            if job is None or job.get("openid") != openid:
                return None
            updated = {**job, "deleted_at": job.get("deleted_at") or time.time(), "updated_at": time.time()}
            if job.get("status") == "processing":
                updated.update(status="failed", error="作品已删除，任务已取消")
            self._persist(updated)
            self._data[job_id] = updated
            return dict(updated)

    def _evict_locked(self, keep: Optional[str] = None) -> List[Dict[str, Any]]:
        """先删过期，再按创建时间删最旧。调用方必须持锁。返回被驱逐的任务。"""
        evicted: List[Dict[str, Any]] = []
        now = time.time()
        for jid in [k for k, v in self._data.items()
                    if now > self._expires_at(v)]:
            evicted.append(self._data.pop(jid))
        while len(self._data) > self._max:
            oldest = min(
                (k for k in self._data if k != keep and self._data[k].get("status") != "processing"),
                key=lambda k: self._data[k].get("created_at", 0),
                default=None,
            )
            if oldest is None:
                break
            evicted.append(self._data.pop(oldest))
        self._delete_rows([j.get("id") for j in evicted if j.get("id")])
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
            evicted = self._startup_evicted + self._evict_locked()
            self._startup_evicted = []
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

    def purge_summary(self) -> Dict[str, int]:
        with self._lock:
            return {"jobs": len(self._data), "processing": sum(
                1 for job in self._data.values()
                if job.get("status") == "processing" and not job.get("deleted_at"))}

    def take_all_for_purge(self) -> List[Dict[str, Any]]:
        """停掉新账号受理后清空任务索引；返回快照供失败恢复与成功后的媒体清理。"""
        with self._lock:
            if any(j.get("status") == "processing" and not j.get("deleted_at")
                   for j in self._data.values()):
                raise ValueError("仍有任务处理中，请完成后再删除全部账号")
            snapshot = [dict(job) for job in self._data.values()]
            self._delete_rows([job["id"] for job in snapshot])
            self._data.clear()
            return snapshot

    def restore_purged(self, snapshot: List[Dict[str, Any]]) -> None:
        with self._lock:
            for job in snapshot:
                self._persist(job)
                self._data[job["id"]] = dict(job)

    def cleanup_purged(self, snapshot: List[Dict[str, Any]]) -> None:
        self._notify_evicted(snapshot)

    def list_for_openid(self, openid: str, offset: int = 0, limit: int = 50) -> List[Dict[str, Any]]:
        """按 openid 筛选当前用户的任务列表：按创建时间倒序。"""
        with self._lock:
            matched = [
                dict(j) for j in self._data.values()
                if j.get("openid") == openid and not j.get("deleted_at")
            ]
            matched.sort(key=lambda j: j.get("created_at", 0), reverse=True)
            return matched[offset:offset + limit]


def _cleanup_job_files(evicted_jobs: List[Dict[str, Any]]) -> None:
    """Invalidate deliverables locally and persist all remote deletions for retry."""
    for job in evicted_jobs:
        if not job:
            continue
        if job.get("status") == "processing":
            users.refund_job(job.get("openid", ""), job["id"])
        for field in ("orig_file", "result_file", "comparison_file"):
            name = job.get(field)
            if name:
                cleanup.schedule("local", name, time.time())
                _safe_remove(os.path.join(UPLOAD_DIR, name))
        for field in ("orig_cos", "result_cos", "norm_cos", "comparison_cos"):
            if job.get(field):
                cleanup.schedule("cos", job[field], time.time())


def _startup_file_gc() -> None:
    """Independent retention clocks: originals 24h, completed results 30 days."""
    now = time.time()
    for path in glob.glob(os.path.join(UPLOAD_DIR, "*")):
        base = os.path.basename(path)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if base.startswith(("incoming_", "tmp_", "norm_", "orig_")):
            ttl = ORIGINAL_TTL_SECONDS
        elif base.startswith(("result_", "compare_")):
            ttl = JOB_TTL_SECONDS
        else:
            continue
        if mtime < now - ttl:
            _safe_remove(path)
    # Register pre-existing job objects as well as newly created ones.
    items, _ = jobs.list_recent(0, 10 ** 6)
    for job in items:
        if job.get("deleted_at"):
            _cleanup_job_files([job])
            continue
        orig_due = float(job.get("created_at") or now) + ORIGINAL_TTL_SECONDS
        result_due = float(job.get("completed_at") or job.get("created_at") or now) + JOB_TTL_SECONDS
        if job.get('cloud_pipeline') and job.get('status')=='processing':
            result_due=float(job.get('created_at') or now)+2*JOB_TTL_SECONDS
        for kind, field, due in (("local", "orig_file", orig_due), ("cos", "orig_cos", orig_due),
                               ("cos", "norm_cos", result_due if job.get('cloud_pipeline') or job.get('comparison_cos')==job.get('norm_cos') else orig_due),
                                 ("local", "comparison_file", result_due), ("cos", "comparison_cos", result_due),
                                 ("local", "result_file", result_due), ("cos", "result_cos", result_due)):
            if job.get(field):
                cleanup.schedule(kind, job[field], time.time() if field.startswith("result_") and job.get("status") == "failed" else due)


def _bg_sweeper() -> None:
    while not _sweeper_stop.wait(30):
        try:
            jobs.sweep()
            _startup_file_gc()
            cleanup.run(settings)
        except Exception:
            log.exception("后台任务清理失败")


jobs = JobStore(JOB_TTL_SECONDS, JOB_MAX_ENTRIES, on_evict=_cleanup_job_files)
cloud = CloudPipeline(lambda: sys.modules[__name__])


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


@image_limited
def _validate_image(path: str) -> None:
    """确认落盘的是真图片，并检查像素规模。"""
    import cv2  # 延迟导入，加快启动

    if os.path.getsize(path) == 0:
        raise HTTPException(status_code=400, detail="上传文件为空")
    try:
        from PIL import Image
        with Image.open(path) as image:
            w, h = image.size
            image.verify()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="无法识别的图片格式") from exc
    if w * h > MAX_PIXELS:
        raise HTTPException(
            status_code=413,
            detail="图片像素过大（上限 %d 万像素），请先缩小。" % (MAX_PIXELS // 10_000),
        )
    if cv2.imread(path, cv2.IMREAD_REDUCED_COLOR_8) is None:
        raise HTTPException(status_code=400, detail="无法识别的图片格式")


@image_limited
def _validate_output(path: str) -> Tuple[int, int]:
    _validate_image(path)
    import cv2
    decoded = cv2.imread(path, cv2.IMREAD_COLOR)
    if decoded is None:
        raise ValueError("增强结果不是可解码的图片")
    h, w = decoded.shape[:2]
    return w, h


@image_limited
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


def _media_expires_at(job: Dict[str, Any], kind: str) -> float:
    origin = float(job.get("created_at") or 0)
    return origin + ORIGINAL_TTL_SECONDS if kind == "orig" and not (job.get('comparison_cos') or job.get('comparison_file')) else \
        float(job.get("completed_at") or origin) + JOB_TTL_SECONDS


def _media_signature(job: Dict[str, Any], filename: str, expiry: int) -> str:
    key = hashlib.sha256(("rescue-media:" + ensure_admin_password()).encode("utf-8")).digest()
    payload = "%s|%s|%s|view|%d" % (filename, job["id"], job["openid"], expiry)
    return hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _job_media_url(job: Dict[str, Any], kind: str) -> Optional[str]:
    if not job or job.get("deleted_at") or time.time() >= _media_expires_at(job, kind):
        return None
    if kind == "result" and job.get("status") != "succeeded":
        return None
    ttl = min(MEDIA_URL_TTL_SECONDS, int(_media_expires_at(job, kind) - time.time()))
    if ttl <= 0:
        return None
    prefix='comparison' if kind=='orig' and (job.get('comparison_cos') or job.get('comparison_file')) else kind
    if job.get(prefix + "_cos") and settings.cos_ready():
        try:
            return cos_presign(settings, "get", job[prefix + "_cos"], ttl_seconds=ttl)
        except CosError:
            pass
    filename = job.get(prefix + "_file")
    if not filename:
        return None
    expiry = int(time.time()) + ttl
    return "/api/images/%s?expires=%d&sig=%s" % (filename, expiry, _media_signature(job, filename, expiry))


def _media_type(path: str) -> str:
    return mimetypes.guess_type(path)[0] or "application/octet-stream"


# --------------------------------------------------------------------------- #
# 处理管线
# --------------------------------------------------------------------------- #
def _run_pipeline(job_id: str, quality: str, style: str,
                  template: Optional[Dict[str, Any]] = None,
                  text_values: Optional[Dict[str, str]] = None,
                  aspect_ratio: str = "", custom_prompt: str = "") -> None:
    """后台执行：归一化 -> 按 settings.chain 依次尝试 -> 本地兜底
    -> 模板输出尺寸 -> 模板文字排版。

    链路顺序、供应商开关、密钥、提示词全部来自运行时设置（/admin 可热改），
    每一档任务执行时现读；模板参数用提交那一刻的快照；
    某一级失败自动落到下一级，本地引擎永远兜底 ——
    就算它被关掉，全链失败时也会强制跑一次。
    """
    job = jobs.get(job_id)
    if job is None or job.get("deleted_at") or job.get("status") != "processing":
        return
    clock_started=time.monotonic()
    timings={"queue_ms":max(0,round((time.time()-float(job.get("created_at") or time.time()))*1000))}
    jobs.update(job_id,started_at=time.time(),stage="normalize")

    src = os.path.join(UPLOAD_DIR, job["orig_file"])
    out = os.path.join(UPLOAD_DIR, job["result_file"])
    # 归一化与中间结果都用 JPEG：体积小、上传快，肉眼无差别
    norm = os.path.join(UPLOAD_DIR, "norm_%s.jpg" % job_id)
    tmp = os.path.join(UPLOAD_DIR, "tmp_%s.jpg" % job_id)

    stage = "normalize"
    provider_used = None
    template = template or {}
    if template:
        aspect_ratio = ""
    try:
        # --- 1. 归一化（包含画幅裁剪）---
        phase=time.monotonic()
        cloud_key=None
        if settings.cos_ready() and job.get('orig_cos') and settings.snapshot()['processing']['ci_enabled']:
            from PIL import Image
            with Image.open(src) as image:
                width,height=image.size
                if image.getexif().get(274) in (6,8):width,height=height,width
            cloud_key='comparisons/%s/%s.jpg'%((job.get('openid') or 'anon')[:8],job_id)
            cleanup.schedule('cos',cloud_key,time.time()+2*JOB_TTL_SECONDS)
            jobs.update(job_id,comparison_cos=cloud_key)
            cos_process_image(settings,job['orig_cos'],cloud_key,
                normalization_rule(width,height,settings.normalize_long_side(),aspect_ratio))
            with open(norm,'wb') as fh:fh.write(cos_get(settings,cloud_key,max_bytes=MAX_UPLOAD_BYTES))
            _validate_output(norm)
        else:
            _normalize_long_side(src,norm,target=settings.normalize_long_side(),aspect_ratio=aspect_ratio)
        compare_file='compare_%s.jpg'%job_id
        shutil.copyfile(norm,os.path.join(UPLOAD_DIR,compare_file))
        jobs.update(job_id,comparison_file=compare_file,comparison_cos=cloud_key,
                    processing_mode='ci' if cloud_key else 'local_serial')
        cleanup.schedule('local',compare_file,time.time()+2*JOB_TTL_SECONDS)
        timings['normalize_ms']=round((time.monotonic()-phase)*1000)
        jobs.update(job_id, stage="enhance")

        # 归一化图上传 COS 并生成签名直链：让网关自己来拉，
        # 服务器→供应商的公网出方向流量归零（同地域内网上传免费）
        gateway_url = None
        if cloud_key:
            gateway_url=cos_presign(settings,'get',cloud_key,ttl_seconds=3600)
            jobs.update(job_id,norm_cos=cloud_key)
        elif settings.cos_ready():
            try:
                norm_key = "norms/%s/%s.jpg" % ((job.get("openid") or "anon")[:8],
                                                job_id)
                cleanup.schedule("cos", norm_key, time.time() + 2*JOB_TTL_SECONDS)
                with open(norm, "rb") as fh:
                    cos_put(settings, norm_key, fh.read())
                gateway_url = cos_presign(settings, "get", norm_key,
                                          ttl_seconds=3600)
                jobs.update(job_id,norm_cos=norm_key,comparison_cos=norm_key)
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
        if custom_prompt and not template:
            tier_prompt += "\n用户修复需求：" + custom_prompt

        # 自定义要求必须由支持提示词的编辑引擎处理，不能静默退化为忽略要求的超分/本地引擎。
        requires_prompt = bool((custom_prompt and not template) or template.get("requires_prompt"))
        chain = ["worldcodes"] if requires_prompt else settings.chain()
        phase=time.monotonic()
        for name in chain:
            if enhanced:
                break
            if name == "local":
                continue  # 本地引擎是最后兜底，见链尾
            if not settings.provider_enabled(name):
                log.info("[%s] %s 已在后台停用，跳过", job_id, name)
                continue
            client = _get_client(name, quality)
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
                _validate_output(tmp)
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
            if requires_prompt:
                raise RuntimeError("指定成品形式或补充要求的生成引擎本次未完成，请重试；本次光子将退回")
            log.info("[%s] 外部链路全部失败，走本地引擎（quality=%s, style=%s）",
                     job_id, quality, style)
            # 前面已经归一化过，这里关掉 2K 上采样，避免把 1536 插值回 2000
            with IMAGE_LOCK:engine.process(norm,tmp,quality=quality,upscale_2k=False,style=style)
            provider_used = "local"

        timings['provider_ms']=round((time.monotonic()-phase)*1000)
        phase=time.monotonic()
        current = jobs.get(job_id)
        if not current or current.get("deleted_at"):
            raise RuntimeError("作品已删除，停止交付")

        # --- 3.5 几何画幅守恒：校验模型生成图与输入图比例，若有偏差做居中对齐，杜绝对比滑块双图错位 ---
        try:
            with IMAGE_LOCK:
                import cv2
                _n_img = cv2.imread(norm)
                _t_img = cv2.imread(tmp)
                if not template and _n_img is not None and _t_img is not None:
                    nh, nw = _n_img.shape[:2]
                    th, tw = _t_img.shape[:2]
                    norm_ratio = nw / float(nh) if nh > 0 else 1.0
                    tmp_ratio = tw / float(th) if th > 0 else 1.0
                    if abs(norm_ratio - tmp_ratio) > 0.015:
                        log.info("[%s] 模型生成画幅(%.3f)与输入(%.3f)存在偏差，自动进行像素级居中对齐",
                                 job_id, tmp_ratio, norm_ratio)
                        _t_cropped = _crop_aspect_ratio(_t_img, "%d:%d" % (nw, nh))
                        cv2.imwrite(tmp, _t_cropped, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
        except Exception as _ar_exc:
            log.warning("[%s] 画幅几何对齐检查异常: %s", job_id, _ar_exc)

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
                with IMAGE_LOCK:apply_text_overlay(tmp,tmp,layout,spec,merged)
                log.info("[%s] 文字排版完成（%s，%d 项）",
                         job_id, layout, len(merged))

        # --- 6. 输出 ---
        _finalize(tmp, out)
        width, height = _validate_output(out)
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
        current = jobs.get(job_id)
        if not current or current.get("deleted_at"):
            raise RuntimeError("作品已删除，停止交付")
        timings['finalize_ms']=round((time.monotonic()-phase)*1000)
        phase=time.monotonic()
        result_key = job.get("result_cos")
        if settings.cos_ready() and not result_key:
            result_key = "results/%s/%s.jpg" % (job["openid"][:8], job_id)
            jobs.update(job_id, result_cos=result_key)
            job["result_cos"] = result_key
        if result_key:
            stage = "store_cos"
            jobs.update(job_id, stage=stage)
            cleanup.schedule("cos", result_key, time.time() + 2 * JOB_TTL_SECONDS)
            with open(out, "rb") as fh:
                payload = fh.read()
            for attempt in range(2):
                try:
                    cos_put(settings, result_key, payload)
                    if not cos_head(settings, result_key):
                        raise CosError("结果上传后未能确认 COS 对象存在")
                    break
                except CosError as exc:
                    # PUT 响应丢失但对象已保存时，不误判交付失败。
                    if cos_head(settings, result_key):
                        break
                    if attempt:
                        raise CosError("结果保存到 COS 失败，请重试；本次光子将退回", code="RESULT_STORAGE_FAILED") from exc
                    log.warning("[%s] 结果存储失败，重试一次: %s", job_id, exc)
        timings['storage_ms']=round((time.monotonic()-phase)*1000)
        timings['processing_ms']=round((time.monotonic()-clock_started)*1000)
        jobs.update(job_id, status="succeeded", stage="done",timings=timings,
                    provider=provider_used, completed_at=time.time(), width=width, height=height)
        current = jobs.get(job_id)
        if not current or current.get("deleted_at"):
            raise RuntimeError("作品已删除，停止交付")
        users.complete_charge(job_id)
        if template.get('id'):
            try:templates.inc_usage(template['id'])
            except Exception:log.exception('作品已交付，模板热度写入稍后需核对')
        cleanup.schedule("local", job["result_file"], current["completed_at"] + JOB_TTL_SECONDS)
        if current.get("result_cos"):
            cleanup.schedule("cos", current["result_cos"], current["completed_at"] + JOB_TTL_SECONDS)
        cleanup.schedule('local',compare_file,current['completed_at']+JOB_TTL_SECONDS)
        if current.get('comparison_cos'):cleanup.schedule('cos',current['comparison_cos'],current['completed_at']+JOB_TTL_SECONDS)
        users.audit(job.get("openid", ""), "completed", "job=%s" % job_id)
        log.info("[%s] 任务完成 -> %s", job_id, os.path.basename(out))

    except Exception as exc:  # noqa: BLE001
        log.exception("[%s] 处理失败（stage=%s）", job_id, stage)
        try:
            timings['processing_ms']=round((time.monotonic()-clock_started)*1000)
            jobs.update(job_id, status="failed", error=str(exc)[:500], stage=stage,timings=timings)
        finally:
            _refund_charged(job.get("openid", ""), job_id,
                            int(job.get("price") or 0))
            _safe_remove(out)
            if job.get("result_cos"):
                cleanup.schedule("cos", job["result_cos"], time.time())
            failed=jobs.get(job_id) or {}
            for field in ('comparison_cos','norm_cos'):
                if failed.get(field):cleanup.schedule('cos',failed[field],time.time())
            if failed.get('comparison_file'):_safe_remove(os.path.join(UPLOAD_DIR,failed['comparison_file']))
    finally:
        current = jobs.get(job_id)
        if current and current.get("deleted_at"):
            _cleanup_job_files([current])
        for path in (norm, tmp):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass


@image_limited
def _finalize(src: str, dst: str) -> None:
    """把结果统一成 JPEG 输出。

    已经由上游编码好的 JPEG 直接搬，不重编码 —— 二次编码只会白白掉画质。
    只有 PNG / WebP 这类才重新编码一次。
    """
    import cv2

    if not os.path.exists(src) or os.path.getsize(src) == 0:
        raise RuntimeError("增强结果为空")
    _validate_output(src)

    with open(src, "rb") as fh:
        magic = fh.read(3)
    if magic == b"\xff\xd8\xff":          # 已经是 JPEG
        shutil.copyfile(src, dst)
        return

    img = cv2.imread(src, cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError("增强结果不是可解码的图片")
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
    if not ok:
        raise RuntimeError("结果编码失败")
    buf.tofile(dst)


@image_limited
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
    gateway_ok = any(bool(settings.gateway_for(q).get('base_url') and settings.gateway_for(q).get('api_key')) for q in ('light','fine'))
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
        "source_revision": SOURCE_REVISION,
        "generation_queue": pool.snapshot(),
        "cloud_pipeline": cloud.snapshot(),
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
            "active_engine": "ci_object" if cloud.enabled() and mod.get('enabled') else ("wechat_free" if wx_ready else ("tencent_ims" if tc_ready else "none")),
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
    """小程序启动时拉取：价格、维护状态、风格表、调试免扣费开关、社区与广告开关。"""
    ads = settings.ads()
    community = CommunityStore(settings)._normalize()
    return {
        "prices": settings.prices(),
        "rewards": settings.rewards(),
        "free_mode": settings.free_mode(),
        "cos_ready": settings.cos_ready(),
        "template_output_modes": ["template", "single"],
        "cloud_pipeline": cloud.snapshot(),
        "template_quality_options": ["light", "fine"],
        "text_generation": {'ready':text_generation_ready(sys.modules[__name__]),'price':settings.snapshot()['text_generation']['price']},
        "image_processing": {"ci_enabled":settings.snapshot()["processing"]["ci_enabled"],"local_image_parallelism":1},
        "maintenance": settings.maintenance(),
        "styles": settings.styles(),
        # 社区（灵感沙龙）：enabled=False 时小程序端隐藏 tab 与入口
        "community": {"enabled": community["enabled"]},
        # 激励视频广告：ready=False 时小程序端不显示"看视频补给"入口
        "ads": {
            "rewarded_video_enabled": ads["rewarded_video_enabled"],
            "rewarded_video_unit_id": ads["rewarded_video_unit_id"],
            "rewarded_video_ready": ads["rewarded_video_ready"],
        },
    }


@app.get("/api/community")
def public_community() -> Dict[str, Any]:
    """Only published posts are returned; pause/delete operate per post."""
    items = []
    for idx, raw in enumerate(CommunityStore(settings).list(status="published", limit=200)["items"]):
        items.append({
            "id": str(raw.get("id") or "c%d" % (idx + 1)),
            "title": str(raw.get("title") or ""),
            "story": str(raw.get("story") or ""),
            "authorName": str(raw.get("author_name") or ""),
            "authorAvatar": str(raw.get("author_avatar") or ""),
            "date": str(raw.get("date") or ""),
            "category": str(raw.get("category") or "all"),
            "categoryName": str(raw.get("category_name") or ""),
            "templateId": str(raw.get("template_id") or ""),
            "templateName": str(raw.get("template_name") or ""),
            "quality": "fine" if str(raw.get("quality")) == "fine" else "light",
            "resultUrl": str(raw.get("result_url") or ""),
            "origUrl": str(raw.get("orig_url") or ""),
            "likes": _community_likes(raw.get("likes")),
            "liked": False,
            "pinned": bool(raw.get("pinned")),
        })
    return {"enabled": True, "items": items,
            "featured_reward": settings.rewards()["community_featured"]}


@app.get("/api/community/media/{filename}")
def community_media(filename: str):
    if not re.fullmatch(r"[0-9a-f]{32}\.jpg", filename):
        raise HTTPException(status_code=404, detail="图片不存在")
    path = os.path.join(CommunityStore(settings).media_dir, filename)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="图片不存在")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=3600"})


def _community_likes(value: Any) -> int:
    try:
        return max(0, min(1_000_000_000, int(value or 0)))
    except (TypeError, ValueError, OverflowError):
        return 0


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
        cloud.audits.wechat_callback(trace_id, suggest)
        hit = wechat_sec.resolve_pending(trace_id, suggest, label, score)
        log.info("微信审核推送: trace=%s suggest=%s label=%s 命中=%s",
                 trace_id, suggest, label, hit)
    return PlainTextResponse("success")


@app.post("/api/callbacks/cos-audit")
async def cos_audit_callback(request: Request):
    """Only private outstanding capability objects can affect cloud audits."""
    length=request.headers.get('content-length') or '0'
    if not length.isdigit():
        raise HTTPException(400, detail='回调长度无效')
    if int(length) > 262144:
        raise HTTPException(413, detail='回调内容过大')
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 262144:
            raise HTTPException(413, detail='回调内容过大')
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, detail='回调 JSON 无效')
    # Console probes/unknown notifications are acknowledged without mutating jobs.
    cloud.audits.cos_callback(body)
    return PlainTextResponse('success')


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
    返回的模板 engine 仅为旧配置参考；实际档位由请求 quality 决定。
    模板文字字段做长度截断 + 默认值补齐（collect_values）。
    """
    template_id = (template_id or "").strip()
    if not template_id:
        return None, "", {}
    tpl = templates.get_template(template_id, enabled_only=True)
    if tpl is None:
        raise HTTPException(status_code=400,
                            detail="模板不存在或已下架，刷新后重试")
    # 兼容返回旧模板元数据，不覆盖用户选择。
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
    app_id: str = ""


class _EarnBody(BaseModel):
    kind: str = ""
    receipt: str = ""


class _UploadBody(BaseModel):
    filename: str = "photo.jpg"
    byte_size: int = 0
    byteSize: Optional[int] = None  # Compatibility with the previous mini-program.


class _InviteBody(BaseModel):
    code: str = ""


class _RescueByUploadBody(BaseModel):
    upload_id: str = ""
    quality: str = ""
    style: str = ""
    template_id: str = ""
    text_fields: str = ""
    aspect_ratio: str = ""
    custom_prompt: str = ""
    template_output_mode: str = "template"


class _ViolationFeedbackBody(BaseModel):
    violation_id: str = ""
    message: str = ""


class _ProfileBody(BaseModel):
    nickname: str = ""


def _login_response(openid: str, account_type: str = "wechat", app_id: str = "") -> Dict[str, Any]:
    try:
        user = users.ensure_user(openid, account_type=account_type, app_id=app_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    users.audit(openid, "login", "source=%s app_id=%s" % (account_type, app_id))
    return {
        "token": user_token(openid),
        "user": {"openid_masked": openid[:6] + "***",
                 "total_jobs": user["total_jobs"], "user_id": user["user_id"],
                 "account_type": account_type, "app_id": app_id},
        "user_id": user["user_id"],
        "account_type": account_type,
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
    app_id = str(settings.wechat().get("app_id") or "")
    if body.app_id and app_id and body.app_id != app_id:
        raise HTTPException(status_code=400, detail="小程序 AppID 与后端配置不一致")
    try:
        openid = code2session(code, settings)
    except WechatAuthError as exc:
        raise HTTPException(status_code=401 if exc.code != "NOT_CONFIGURED" else 503,
                            detail=str(exc)) from exc
    if app_id != str(settings.wechat().get("app_id") or ""):
        raise HTTPException(status_code=409, detail="登录期间配置发生变化，请重新登录")
    return _login_response(openid, account_type="wechat", app_id=app_id)


@app.post("/api/auth/web")
def web_login(request: Request, response: Response):
    """A browser session is a web visitor, never a WeChat user or an IP account."""
    token = bearer_of({k.lower(): v for k, v in request.headers.items()})
    bearer_user = verify_user_token(token) if token else None
    existing = users.get_user(bearer_user) if bearer_user else None
    if existing and existing.get("account_type") == "web":
        openid = bearer_user  # Preserve an authenticated legacy account and balance.
    else:
        openid = verify_web_identity(request.cookies.get("rescue_web_identity", ""))
        stored = users.get_user(openid) if openid else None
        if stored and stored.get("account_type") != "web":
            openid = None
        openid = openid or "web-" + uuid.uuid4().hex
    data = _login_response(openid, account_type="web")
    response.set_cookie("rescue_web_identity", make_web_identity(openid), max_age=WEB_IDENTITY_TTL,
                        httponly=True, secure=request.url.scheme == "https", samesite="lax", path="/api/auth/web")
    return data


@app.get("/api/me")
def get_me(request: Request):
    """当前用户资料：光子余额、邀请码、今日奖励进度、封禁状态。"""
    user = _current_user(request)
    openid = user["openid"]
    return {
        "openid_masked": openid[:6] + "***",
        "user_id": user["user_id"],
        "account_type": user["account_type"],
        "balance": user["balance"],
        "total_jobs": user["total_jobs"],
        "invite_code": user["invite_code"],
        "nickname": user.get("nickname", ""),
        "banned": user.get("banned", False),
        "earn": {
            **users.checkin_status(openid),
            "checkin_reward": settings.rewards()["checkin"],
            "checkin_seventh_bonus": settings.rewards()["checkin_seventh_bonus"],
            "video_today": users.earn_count_today(openid, "video"),
        },
    }


@app.post("/api/me/profile")
def update_profile(body: _ProfileBody, request: Request):
    user = _current_user(request)
    nickname = body.nickname.strip()
    if not nickname or len(nickname) > 24 or any(ord(ch) < 32 for ch in nickname):
        raise HTTPException(status_code=400, detail="昵称需为 1～24 字")
    reject = _moderate_text_or_reject(nickname, user["openid"])
    if reject:
        raise HTTPException(status_code=400, detail=reject)
    return {"ok": True, "nickname": users.set_nickname(user["openid"], nickname)}


@app.post("/api/me/violation-feedback")
def violation_feedback(body: _ViolationFeedbackBody, request: Request):
    user = _current_user(request)
    message = body.message.strip()
    if not message or len(message) > 500:
        raise HTTPException(status_code=400, detail="反馈内容需为 1～500 字")
    if not users.submit_violation_feedback(user["openid"], body.violation_id, message):
        raise HTTPException(status_code=404, detail="违规记录不存在或已经反馈")
    return {"ok": True, "message": "反馈已提交，等待人工复核"}


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
    # 看视频奖励必须建立在真实广告之上：广告位没配好就不发奖，
    # 否则前端一点就凭空到账（此前正是这个漏洞）。
    if kind == "video" and not settings.ads()["rewarded_video_ready"]:
        raise HTTPException(status_code=403,
                            detail="激励视频广告未配置，暂不可领取")
    if kind == "checkin":
        rewards = settings.rewards()
        claim = users.claim_checkin(user["openid"], rewards["checkin"],
                                    rewards["checkin_seventh_bonus"])
        if not claim["claimed"]:
            raise HTTPException(status_code=429, detail="今日签到奖励已领完，明天再来吧")
        return {"ok": True, "kind": kind, "balance": claim["balance"],
                "reward": claim["reward"], "bonus_awarded": claim["bonus_awarded"],
                "checkin_streak": claim["checkin_streak"],
                "checkin_progress": claim["checkin_progress"], "count_today": 1}
    if kind == "video":
        try:
            event_id = verify_video(settings, user["openid"], body.receipt)
            ok, balance, count = users.claim_video_reward(user["openid"], event_id,
                                                         conf["reward"], conf["limit"])
        except ValueError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
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
    try:
        reward = settings.rewards()["invite"]
        my_balance, inviter_balance = users.bind_invite_once(openid, code, reward=reward)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "balance": my_balance, "reward": reward,
            "inviter_balance": inviter_balance}





@app.post("/api/uploads")
def create_upload(payload: _UploadBody, request: Request):
    """申请 COS 预签名直传地址。未配置 COS 时返回 503。"""
    user = _current_user(request)
    filename = payload.filename
    byte_size = payload.byteSize if payload.byteSize is not None else payload.byte_size
    if not settings.cos_ready():
        raise HTTPException(status_code=503, detail="对象存储未配置")
    ext = os.path.splitext(filename or "")[1].lower() or ".jpg"
    if ext not in ALLOWED_EXT:
        raise HTTPException(status_code=400,
                            detail="仅支持 JPG / PNG / WebP / BMP / TIFF")
    if byte_size > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="图片不能超过 %d MB"
                            % (MAX_UPLOAD_BYTES // 1024 // 1024))
    if byte_size < 0:
        raise HTTPException(status_code=400, detail="图片大小必须是非负整数")
    upload_id = uuid.uuid4().hex[:16]
    key = "uploads/%s/%s%s" % (user["openid"][:8], upload_id, ext)
    url = cos_presign(settings, "put", key, ttl_seconds=3600)
    cleanup.schedule("cos", key, time.time() + 3600)
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
    if rec is None or rec["openid"] != user["openid"] or time.time() - rec["created_at"] > 3600:
        raise HTTPException(status_code=404, detail="上传登记不存在")
    if not cos_head(settings, rec["key"]):
        raise HTTPException(status_code=400,
                            detail="COS 上还没有这个文件，先完成直传")
    return {"ok": True, "key": rec["key"]}


def _chosen_quality(req_q: str, tpl_quality: str) -> str:
    """模板与纯修复共用用户选择，缺省 light。"""
    if req_q in ("light", "fine"):
        return req_q
    if req_q:raise HTTPException(status_code=400,detail='生成档位必须为 light 或 fine')
    return "light"


@app.post("/api/rescue")
@upload_limited
def create_rescue_job(
    request: Request,
    image: UploadFile = File(...),
    quality: str = Form("light"),
    style: str = Form(""),
    template_id: str = Form(""),
    text_fields: str = Form(""),
    aspect_ratio: str = Form(""),
    custom_prompt: str = Form(""),
    template_output_mode: str = Form("template"),
):
    """提交修图任务（multipart 路径；启用 COS 直传后小程序走 /by-upload）。

    支持画幅比例 aspect_ratio（1:1, 3:4, 4:3, 9:16, 16:9, original）。
    template_id 非空时引擎档位/提示词/输出尺寸/文字排版全部以模板为准；
    纯修复模式用用户显式选择的 quality（light/fine）。

    同步处理：cv2 解码、内容审核、COS 上传全是阻塞 IO，
    FastAPI 会把整个 handler 丢进线程池，事件循环不被拖住。
    """
    user = _rescue_guard(request)
    if cloud.enabled():
        raise HTTPException(status_code=503, detail='云端模式请使用 COS 直传，不经后端上传图片')
    tpl, tpl_quality, text_values = _resolve_template(template_id, text_fields)
    tpl = _apply_template_output(tpl, template_output_mode)
    quality, style = _validate_quality_style(
        _chosen_quality((quality or "").strip().lower(), tpl_quality), style)
    custom_prompt, combined_text = _user_text(custom_prompt, text_values, tpl)

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

    text_reject = _moderate_text_or_reject(combined_text, user["openid"])
    if text_reject:
        _safe_remove(job_tmp)
        _reject_uploaded_content(user["openid"], "text", text_reject, quality, tpl)
    # 上传侧内容审核：AI 调用之前拦，省钱也合规
    with open(job_tmp, "rb") as fh:
        reject = _moderate_or_reject(fh.read(), "upload", user["openid"])
    if reject:
        _safe_remove(job_tmp)
        _reject_uploaded_content(user["openid"], "image", reject, quality, tpl)

    try:
        return _register_job(user["openid"], quality, style, job_tmp, ext,
                             template=tpl, text_values=text_values,
                             aspect_ratio=aspect_ratio, custom_prompt=custom_prompt)
    except Exception:
        _safe_remove(job_tmp)  # 含 402 光子不足等失败路径，别留下孤儿临时文件
        raise


@app.post("/api/rescue/by-upload")
@partial(upload_limited, should_limit=lambda: not cloud.enabled())
def create_rescue_job_by_upload(payload: _RescueByUploadBody,
                                request: Request):
    """COS 直传路径。云端模式只登记元数据，由 CI/供应商/COS 完成图片处理；
    明确关闭云端模式时保留原有同步链路，不在失败时自动切换链路。"""
    user = _rescue_guard(request)
    upload_id = (payload.upload_id or "").strip()
    tpl, tpl_quality, text_values = _resolve_template(
        payload.template_id, payload.text_fields)
    tpl = _apply_template_output(tpl, payload.template_output_mode)
    quality, style = _validate_quality_style(
        _chosen_quality((payload.quality or "").strip().lower(), tpl_quality),
        payload.style)
    custom_prompt, combined_text = _user_text(payload.custom_prompt, text_values, tpl)

    with _uploads_lock:
        rec = _uploads.get(upload_id)
        if rec is None or rec["openid"] != user["openid"] or time.time() - rec["created_at"] > 3600:
            raise HTTPException(status_code=404, detail="上传登记不存在或已过期")
        _uploads.pop(upload_id)
    if cloud.enabled():
        try:
            text_reject = _moderate_text_or_reject(combined_text, user['openid'])
            if text_reject:_reject_uploaded_content(user['openid'], 'text', text_reject, quality, tpl)
            return cloud.admit(user['openid'],quality,style,source=rec,template=tpl,
                               text_values=text_values,aspect_ratio=payload.aspect_ratio,custom_prompt=custom_prompt)
        except Exception:
            with _uploads_lock:_uploads.setdefault(upload_id,rec)
            raise
    if not cos_head(settings, rec["key"]):
        raise HTTPException(status_code=400, detail="COS 上没有这个文件")

    ext = rec["ext"]
    job_tmp = os.path.join(UPLOAD_DIR,
                           "incoming_%s%s" % (uuid.uuid4().hex[:8], ext))
    try:
        data = cos_get(settings, rec["key"], max_bytes=MAX_UPLOAD_BYTES)
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="图片超过上传大小上限")
        with open(job_tmp, "wb") as fh:
            fh.write(data)
        _validate_image(job_tmp)
    except CosError as exc:
        _safe_remove(job_tmp)
        raise HTTPException(status_code=413 if exc.code == "TOO_LARGE" else 502,
                            detail="图片超过上传大小上限" if exc.code == "TOO_LARGE" else "对象存储读取失败") from exc
    except HTTPException:
        _safe_remove(job_tmp)
        raise
    finally:
        cleanup.schedule("cos", rec["key"], time.time())

    text_reject = _moderate_text_or_reject(combined_text, user["openid"])
    if text_reject:
        _safe_remove(job_tmp)
        _reject_uploaded_content(user["openid"], "text", text_reject, quality, tpl)
    with open(job_tmp, "rb") as fh:
        reject = _moderate_or_reject(fh.read(), "upload", user["openid"])
    if reject:
        _safe_remove(job_tmp)
        _reject_uploaded_content(user["openid"], "image", reject, quality, tpl)

    try:
        return _register_job(user["openid"], quality, style, job_tmp, ext,
                             template=tpl, text_values=text_values,
                             aspect_ratio=payload.aspect_ratio, custom_prompt=custom_prompt)
    except Exception:
        _safe_remove(job_tmp)
        raise


@app.get("/api/jobs/{job_id}")
def query_job_status(job_id: str, request: Request):
    """前端轮询接口。任务只对提交者本人可见（他人查询一律 404，不泄露存在性）。"""
    user = _current_user(request)
    job_id = _safe_job_id(job_id)
    job = jobs.get(job_id)
    if job is None or job.get("deleted_at") or job.get("openid") != user["openid"]:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    return {
        "id": job["id"],
        "status": job["status"],
        "stage": job.get("stage"),
        "cloud_pipeline":bool(job.get('cloud_pipeline')),
        "quality": job.get("quality"),
        "aspect_ratio": job.get("aspect_ratio", ""),
        "template_id": job.get("template_id", ""),
        "template_name": job.get("template_name", ""),
        "template_output_mode": job.get("template_output_mode", "template" if job.get("template_id") else ""),
        "comparison_compressed":bool(job.get("comparison_file") or job.get("comparison_cos")),
        "input_mode":job.get('input_mode','photo'),
        "timings":job.get("timings",{}),
        "started_at":job.get("started_at"),
        "completed_at":job.get("completed_at"),
        "price": job.get("price"),
        "provider": job.get("provider"),
        "width": job.get("width"),
        "height": job.get("height"),
        "balance": users.get_balance(user["openid"]),
        "error": job.get("error"),
        "orig_url": _job_media_url(job, "orig"),
        "result_url": _job_media_url(job, "result"),
        "created_at": job.get("created_at"),
    }


@app.get("/api/credits/packages")
def credit_packages() -> Dict[str, Any]:
    """先公开真实价格表；收款与发货未闭环前绝不由客户端加光子。"""
    cost = settings.prices().get("light", GENERATION_COST)
    return {"packages": public_packages(cost), "points_per_yuan": POINTS_PER_YUAN,
            "generation_cost": cost, "payment_ready": False}


@app.post("/api/jobs/{job_id}/refresh-media")
def refresh_job_media(job_id: str, request: Request, kind: str = "result"):
    """404 修复只返回新 COS 地址，图片补存仅走 COS 内网，不走客户端图片代理。"""
    user = _current_user(request)
    job_id = _safe_job_id(job_id)
    if kind not in ("orig", "result"):
        raise HTTPException(status_code=400, detail="图片类型错误")
    with _media_repair_lock:
        job = jobs.get(job_id)
        if not job or job.get("deleted_at") or job.get("openid") != user["openid"]:
            raise HTTPException(status_code=404, detail="作品不存在")
        if job.get("status") != "succeeded":
            raise HTTPException(status_code=409, detail="作品尚未完成")
        if time.time() >= _media_expires_at(job, kind):
            raise HTTPException(status_code=410, detail="原图已到保存期限" if kind == "orig" else "作品已到保存期限")
        if not settings.cos_ready():
            raise HTTPException(status_code=503, detail="COS 尚未配置完整")
        prefix="comparison" if kind=="orig" and (job.get("comparison_cos") or job.get("comparison_file")) else kind
        key = job.get(prefix + "_cos")
        if key and cos_head(settings, key):
            return {"url": _job_media_url(job, kind), "recovered": False}
        filename = job.get(prefix + "_file")
        path = _resolve_upload(filename) if filename else ""
        if not path or not os.path.isfile(path):
            raise HTTPException(status_code=404, detail="COS 结果缺失，服务器本地副本也不存在" if kind == "result" else "原图文件已缺失")
        # 新对象键避开旧键可能遗留的立即删除队列；不延长原有保存期限。
        ext = os.path.splitext(filename)[1]
        object_prefix = "results" if kind == "result" else ("comparisons" if prefix=='comparison' else "origins")
        recovered_key = "%s/%s/%s_recovered_%s%s" % (object_prefix, user["openid"][:8], job_id, uuid.uuid4().hex[:8], ext)
        expires = _media_expires_at(job, kind)
        cleanup.schedule("cos", recovered_key, expires)
        try:
            with open(path, "rb") as fh:
                cos_put(settings, recovered_key, fh.read(), content_type=_media_type(path), internal_only=True)
            if not cos_head(settings, recovered_key):
                raise CosError("补存后未能确认 COS 对象存在")
        except CosError as exc:
            cleanup.schedule("cos", recovered_key, time.time())
            raise HTTPException(status_code=503, detail="COS 内网补存失败，未使用公网图片中转；请检查同地域内网配置") from exc
        current = jobs.get(job_id)
        if not current or current.get("deleted_at") or time.time() >= expires:
            cleanup.schedule("cos", recovered_key, time.time())
            raise HTTPException(status_code=410, detail="作品已删除或到期")
        jobs.update(job_id, **{prefix + "_cos": recovered_key})
        return {"url": _job_media_url(jobs.get(job_id), kind), "recovered": True}


@app.get("/api/my/jobs")
def get_my_jobs(request: Request, limit: int = 30):
    """查询当前登录用户最近提交的任务历史列表（支持跨端同步与切屏恢复）。"""
    user = _current_user(request)
    raw_list = jobs.list_for_openid(user["openid"], limit=min(max(1, limit), 100))
    res = []
    for job in raw_list:
        res.append({
            "id": job["id"],
            "status": job["status"],
            "stage": job.get("stage"),
            "quality": job.get("quality"),
            "input_mode":job.get('input_mode','photo'),
            "provider": job.get("provider"),
            "width": job.get("width"),
            "height": job.get("height"),
            "aspect_ratio": job.get("aspect_ratio", ""),
            "template_id": job.get("template_id", ""),
            "template_name": job.get("template_name", ""),
            "error": job.get("error"),
            "orig_url": _job_media_url(job, "orig"),
            "result_url": _job_media_url(job, "result"),
            "created_at": job.get("created_at"),
        })
    return {"jobs": res}


@app.delete("/api/my/jobs/{job_id}")
def delete_my_job(job_id: str, request: Request):
    user = _current_user(request)
    job = jobs.delete_for_openid(_safe_job_id(job_id), user["openid"])
    if job is None:
        raise HTTPException(status_code=404, detail="作品不存在")
    if job.get("status") == "failed":
        users.refund_job(user["openid"], job_id)
    _cleanup_job_files([job])
    cos_keys = [job.get(field) for field in ("orig_cos", "result_cos", "norm_cos") if job.get(field)]
    cos_deleted = cleanup.delete_cos_now(settings, cos_keys)
    users.audit(user["openid"], "deleted", "job=" + job_id)
    return {"ok": True, "balance": users.get_balance(user["openid"]),
            "cos_deleted": cos_deleted, "cos_pending": len(cos_keys) - cos_deleted}


@app.delete("/api/my/jobs")
def delete_all_my_jobs(request: Request):
    user = _current_user(request)
    items = jobs.list_for_openid(user["openid"], limit=10 ** 6)
    for item in items:
        delete_my_job(item["id"], request)
    return {"ok": True, "deleted": len(items), "balance": users.get_balance(user["openid"])}


@app.get("/api/images/{filename}")
def get_image(filename: str, request: Request) -> FileResponse:
    path = _resolve_upload(filename)
    match = re.fullmatch(r"(orig|result|compare)_([0-9a-f]{6,32})[.][A-Za-z0-9]+", filename)
    job = jobs.get(match[2]) if match else None
    prefix='comparison' if match and match[1]=='compare' else (match[1] if match else '')
    if not job or job.get("deleted_at") or filename != job.get(prefix + "_file"):
        raise HTTPException(status_code=404, detail="图片不存在")
    kind = match[1]
    if kind == "result" and job.get("status") != "succeeded":
        raise HTTPException(status_code=404, detail="图片尚未完成")
    expires=(float(job.get('created_at') or 0)+ORIGINAL_TTL_SECONDS) if kind=='orig' else _media_expires_at(job,'result')
    if time.time() >= expires or not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="图片已过期或不存在")
    token = bearer_of({k.lower(): v for k, v in request.headers.items()})
    owner = verify_user_token(token) if token else None
    if owner != job["openid"]:
        try:
            expiry = int(request.query_params.get("expires", "0"))
            valid = time.time() < expiry <= time.time() + MEDIA_URL_TTL_SECONDS + 5
            signature = request.query_params.get("sig", "")
            valid = valid and hmac.compare_digest(signature, _media_signature(job, filename, expiry))
        except (ValueError, TypeError):
            valid = False
        if not valid:
            raise HTTPException(status_code=403, detail="图片访问凭据已失效")
    return FileResponse(path, media_type=_media_type(path),
                        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


@app.exception_handler(Exception)
async def unhandled(request, exc):  # noqa: ANN001, ARG001
    log.exception("未处理异常: %s", exc)
    return JSONResponse(status_code=500, content={"detail": "服务器内部错误"})


app.include_router(make_text_router(lambda:sys.modules[__name__]))

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        reload=bool(os.environ.get("RELOAD")),
    )
