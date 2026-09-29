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
from typing import Any, Dict, Optional, Tuple

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
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
    """按当前设置取客户端；配置指纹变化时重建。返回 None 表示不可用。"""
    conf = settings.provider(name)
    fp = _fingerprint(conf)
    with _clients_lock:
        cached = _clients.get(name)
        if cached is not None and cached[0] == fp:
            return cached[1]
    client = _build_client(name, conf)
    with _clients_lock:
        _clients[name] = (fp, client)
    return client


pool = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="rescue")


# --------------------------------------------------------------------------- #
# 用户鉴权 / 直传登记 / 配额 / 审核
# --------------------------------------------------------------------------- #
def _current_user(request) -> Dict[str, Any]:
    """从 Authorization: Bearer 解出 openid 并返回用户行，未登录抛 401。"""
    token = bearer_of({k.lower(): v for k, v in request.headers.items()})
    openid = verify_user_token(token) if token else None
    if not openid:
        raise HTTPException(status_code=401, detail="请先登录")
    return users.ensure_user(openid)


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


def _moderate_or_reject(image_bytes: bytes, job_ctx: str, openid: str) -> Optional[str]:
    """内容审核。返回拒绝原因，None = 放行。

    - 未启用或未配密钥：直接放行；
    - 审核服务异常：默认放行并大声记日志（block_on_error 可改为拦截）；
    - Block / Review 一律拦截（Review 走人审也来不及，先拦住再说）。
    """
    if not settings.moderation_ready():
        return None
    try:
        suggestion, label, score = moderate_image_bytes(image_bytes, settings)
    except ModerationError as exc:
        if settings.moderation().get("block_on_error"):
            users.audit(openid, "moderation_error", str(exc))
            return "安全审核服务异常，已按策略拦截"
        log.warning("[%s] 审核服务异常(%s)，本次放行: %s", job_ctx, exc.code, exc)
        return None
    if suggestion in ("Block", "Review"):
        users.audit(openid, "blocked", "label=%s score=%s" % (label, score))
        users.inc_blocked(openid)
        return "图片内容未通过安全审核(%s)" % label
    return None


def _register_job(openid: str, quality: str, style: str,
                  orig_tmp_path: str, ext: str,
                  template: Optional[Dict[str, Any]] = None,
                  text_values: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """把已落盘的原图登记为任务（ multipart 与 COS 直传共用这条尾巴）。

    template 非空时，引擎档位/提示词/输出尺寸/文字排版全部以模板为准，
    价格用模板价（未定价回退到档位默认价）。
    """
    job_id = uuid.uuid4().hex[:12]
    orig_file = "orig_%s%s" % (job_id, ext)
    orig_path = os.path.join(UPLOAD_DIR, orig_file)
    shutil.move(orig_tmp_path, orig_path)
    result_file = "result_%s.jpg" % job_id

    price = _effective_price(quality, template)
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
        orig_file=orig_file,
        result_file=result_file,
        stage="queued",
        orig_url="/api/images/%s" % orig_file,
        result_url="/api/images/%s" % result_file,
        orig_cos=orig_cos,
        result_cos=result_cos,
    )
    jobs.sweep()
    users.inc_total(openid)
    users.audit(openid, "submitted", "job=%s quality=%s tpl=%s"
                % (job_id, quality, (template or {}).get("id", "-")))
    pool.submit(_run_pipeline, job_id, quality, style,
                copy_template(template), dict(text_values or {}))
    return {
        "code": 0,
        "job_id": job_id,
        "status": "processing",
        "quality": quality,
        "template_id": (template or {}).get("id", ""),
        "template_name": (template or {}).get("name", ""),
        "price": price,
        "orig_url": "/api/images/%s" % orig_file,
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
    """

    def __init__(self, ttl: int, max_entries: int) -> None:
        self._data: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._ttl = ttl
        self._max = max_entries

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
            if len(self._data) > self._max:
                self._evict_locked(keep=job_id)
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

    def _evict_locked(self, keep: Optional[str] = None) -> None:
        """先删过期，再按创建时间删最旧。调用方必须持锁。"""
        now = time.time()
        for jid in [k for k, v in self._data.items()
                    if now - v.get("created_at", now) > self._ttl]:
            self._data.pop(jid, None)
        while len(self._data) > self._max:
            oldest = min(
                (k for k in self._data if k != keep),
                key=lambda k: self._data[k].get("created_at", 0),
                default=None,
            )
            if oldest is None:
                break
            self._data.pop(oldest, None)

    def sweep(self) -> None:
        with self._lock:
            self._evict_locked()

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


jobs = JobStore(JOB_TTL_SECONDS, JOB_MAX_ENTRIES)


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


def _normalize_long_side(src: str, dst: str, target: int = 0) -> str:
    """把长边缩放到 target，输出 JPEG q95。

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
                  text_values: Optional[Dict[str, str]] = None) -> None:
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
        # --- 1. 归一化 ---
        _normalize_long_side(src, norm, target=settings.normalize_long_side())
        jobs.update(job_id, stage="enhance")

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
                    # 模板可覆盖模型（原生 2K/4K）与尺寸参数
                    client.enhance(norm, tmp, quality=quality, style=style,
                                   prompt=tier_prompt,
                                   size=str(template.get("gateway_size") or ""),
                                   model=str(template.get("model_override") or ""))
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
            log.warning("[%s] 结果被审核拦截: %s", job_id, reject)
            return
        jobs.update(job_id, status="succeeded", stage="done",
                    provider=provider_used)
        users.audit(job.get("openid", ""), "completed", "job=%s" % job_id)
        log.info("[%s] 任务完成 -> %s", job_id, os.path.basename(out))

    except Exception as exc:  # noqa: BLE001
        log.exception("[%s] 处理失败（stage=%s）", job_id, stage)
        jobs.update(job_id, status="failed", error=str(exc)[:500], stage=stage)
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
    return {
        "ok": True,
        "gateway": gateway_ok,
        "fal": fal_conf,
        "baidu": baidu_conf,
        "local": True,
        "configured": gateway_ok or fal_conf or baidu_conf,
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


@app.get("/")
def home_page() -> FileResponse:
    """网页端画质修复控制台页面。"""
    return FileResponse(
        os.path.join(BASE_DIR, "index.html"),
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


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
    """所有提交路径共用的前置检查：登录 -> 维护 -> 频控/配额。返回用户行。"""
    user = _current_user(request)
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


def _safe_remove(path: str) -> None:
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


@app.post("/api/auth/login")
async def wechat_login(request: Request):
    """wx.login 的 code 换用户会话 token（12h）。"""
    try:
        raw = await request.body()
        body = json.loads(raw.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="请求体不是合法 JSON") from exc
    code = str(body.get("code") or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="缺少 code")
    try:
        openid = code2session(code, settings)
    except WechatAuthError as exc:
        raise HTTPException(status_code=401 if exc.code != "NOT_CONFIGURED" else 503,
                            detail=str(exc)) from exc
    user = users.ensure_user(openid)
    users.audit(openid, "login")
    return {
        "token": user_token(openid),
        "user": {"openid_masked": openid[:6] + "***",
                 "total_jobs": user["total_jobs"]},
        "free_mode": settings.free_mode(),
        "cos_ready": settings.cos_ready(),
    }


@app.post("/api/uploads")
async def create_upload(request: Request,
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
async def complete_upload(upload_id: str, request: Request):
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


@app.post("/api/rescue")
async def create_rescue_job(
    request: Request,
    image: UploadFile = File(...),
    quality: str = Form("fine"),
    style: str = Form(""),
    template_id: str = Form(""),
    text_fields: str = Form(""),
):
    """提交修图任务（multipart 路径；启用 COS 直传后小程序走 /by-upload）。

    template_id 非空时走模板：引擎/提示词/输出尺寸/文字排版/价格全部以
    模板为准，quality/style 仅作兜底。立刻返回 job_id，处理在线程池里跑，
    避免 15~30s 推理撑爆请求超时。
    前置：登录 -> 维护检查 -> 频控/配额 -> 上传侧内容审核。
    """
    user = _rescue_guard(request)
    tpl, tpl_quality, text_values = _resolve_template(template_id, text_fields)
    quality, style = _validate_quality_style(
        tpl_quality or quality, style)

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
                chunk = await image.read(1024 * 1024)
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
        await image.close()

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

    return _register_job(user["openid"], quality, style, job_tmp, ext,
                         template=tpl, text_values=text_values)


@app.post("/api/rescue/by-upload")
async def create_rescue_job_by_upload(request: Request):
    """COS 直传路径：JSON {upload_id, quality, style, template_id, text_fields}，
    图片字节不过本服务器。"""
    user = _rescue_guard(request)
    try:
        raw = await request.body()
        body = json.loads(raw.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="请求体不是合法 JSON") from exc
    upload_id = str(body.get("upload_id") or "")
    tpl, tpl_quality, text_values = _resolve_template(
        str(body.get("template_id") or ""), str(body.get("text_fields") or ""))
    quality, style = _validate_quality_style(
        tpl_quality or str(body.get("quality") or "fine"),
        str(body.get("style") or ""))

    with _uploads_lock:
        rec = _uploads.get(upload_id)
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

    return _register_job(user["openid"], quality, style, job_tmp, ext,
                         template=tpl, text_values=text_values)


@app.get("/api/jobs/{job_id}")
def query_job_status(job_id: str) -> Dict[str, Any]:
    """前端轮询接口。"""
    job_id = _safe_job_id(job_id)
    job = jobs.get(job_id)
    if job is None:
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
        "template_id": job.get("template_id", ""),
        "template_name": job.get("template_name", ""),
        "price": job.get("price"),
        "provider": job.get("provider"),
        "error": job.get("error"),
        "orig_url": _media_url("orig", job.get("orig_url")),
        "result_url": _media_url("result", job.get("result_url")),
        "created_at": job.get("created_at"),
    }


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
