# -*- coding: utf-8 -*-
"""
fal.ai 图像增强客户端 —— 双档设计（light / fine）

设计对齐「猫猫九命机」的两档结构：
    light → fal-ai/esrgan              轻量保真超分，成本极低（约 ¥0.01/张）
    fine  → fal-ai/clarity-upscaler    生成式修复，能补出滤镜补不出的低对比度细节

密钥不写死在代码里。运行前二选一：
    1) 设置环境变量      FAL_KEY=xxxxxxxx:yyyyyyyy
    2) 写入 backend/.env  （需安装 python-dotenv，requirements 里已包含）

用法：
    from fal_ai import FalImageEnhance, FalError
    client = FalImageEnhance()
    if client.configured:
        client.enhance("in.jpg", "out.jpg", quality="fine")
"""
from __future__ import annotations

import base64
import math
import mimetypes
import os
import time
from typing import Any, Dict, Optional, Tuple

import requests

__all__ = ["FalImageEnhance", "FalError"]


# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
DEFAULT_SUBMIT_TIMEOUT = 30      # 提交任务
DEFAULT_POLL_TIMEOUT = 20        # 单次轮询
DEFAULT_DOWNLOAD_TIMEOUT = 60    # 下载结果图
DEFAULT_MAX_WAIT = 240           # 单张图最长等待（fine 档 30~60s 是常态）
DEFAULT_POLL_INTERVAL = 1.5      # 轮询间隔（秒）

LIGHT_MODEL = "fal-ai/esrgan"
FINE_MODEL = (os.environ.get("FINE_MODEL") or "fal-ai/clarity-upscaler").strip()
FINE_FALLBACK_MODEL = "fal-ai/esrgan"   # clarity 失败时退回，至少保证有输出

# RealESRGAN 权重选择：
#   x4plus      通用照片，默认
#   x2plus      放大 2 倍时用它，比 x4 再缩更干净
#   x4_v3       新版通用权重，细节更自然
#   wdn_v3      强降噪版，暗光噪点重时用
LIGHT_MODEL_ID = "RealESRGAN_x4_v3"


# --------------------------------------------------------------------------- #
# 计费模型
# --------------------------------------------------------------------------- #
# 数据来源：fal.ai 模型页（2026-09 实测抓取）。官方定价页原文：
#   "Image models are billed by either image count or output size in megapixels"
#
# 关键结论：按「输出」像素计费的模型，成本与 upscale_factor 是平方关系 ——
# 放大倍数翻倍，账单翻四倍。提示词不是计费维度，加提示词本身不涨钱。
#
#   output_megapixel   成本 = 输出MP × price
#   compute_second     成本 = GPU 计算秒 × price（与输出尺寸关系较弱）
#   image              成本 = 每张固定价
MODEL_BILLING: Dict[str, Dict[str, Any]] = {
    "fal-ai/esrgan":                {"unit": "compute_second",   "price": 0.00111},
    "fal-ai/clarity-upscaler":      {"unit": "output_megapixel", "price": 0.03},
    "fal-ai/codeformer":            {"unit": "output_megapixel", "price": 0.0021},
    "fal-ai/seedvr/upscale/image":  {"unit": "output_megapixel", "price": 0.001},
    "fal-ai/recraft/upscale/crisp": {"unit": "image",            "price": 0.004},
}

USD_TO_CNY = 7.2

# esrgan 按计算秒计费，单价没法事先算准。保守估 4 秒。
ESRGAN_ASSUMED_SECONDS = 4.0

# 输出像素预算：超过就自动收紧 upscale_factor。
# 设 0 关闭限制（不建议 —— 1536 输入 × factor 4 = 28 MP ≈ ¥6.1/张）。
# 10 MP 对应 clarity 约 ¥2.16/张，是目前默认档的上限。
MAX_OUTPUT_MEGAPIXELS = float(os.environ.get("MAX_OUTPUT_MEGAPIXELS", "10"))

# 修图向提示词：压低「生成感」，拉高「保真度」
FINE_PROMPT = (
    "professional photograph, natural skin texture, sharp details, "
    "realistic lighting, high resolution, true to original"
)
FINE_NEGATIVE_PROMPT = (
    "oversmooth, plastic skin, blurry, painting, oil painting, cartoon, "
    "illustration, CG, 3D render, distorted, deformed, extra fingers, "
    "watermark, text"
)

# 风格 -> 提示词。prompt 不参与计费，所以「加提示词」本身不涨钱；
# 涨钱的是换模型和放大倍数。但提示词越有想法，生成式模型越可能改动
# 原图内容 —— 修图场景的原则是描述「更好的照片」，而不是「另一张照片」。
STYLE_PROMPTS: Dict[str, str] = {
    "ai": FINE_PROMPT,
    "fuji": (
        "professional photograph, classic film look, subtle grain, "
        "soft warm highlights, muted greens, natural skin texture, "
        "true to original"
    ),
    "clear": (
        "professional photograph, clean bright lighting, cool white "
        "balance, clear skin texture, natural colors, true to original"
    ),
    "gym_contrast": (
        "professional fitness photograph, defined muscle contours, "
        "crisp fabric texture, controlled contrast, natural skin "
        "texture, true to original"
    ),
}

_QUEUE_BASE = "https://queue.fal.run"


# --------------------------------------------------------------------------- #
# 异常
# --------------------------------------------------------------------------- #
class FalError(RuntimeError):
    """fal.ai 调用失败。code 用于上层决定是否降级。"""

    def __init__(self, message: str, *, status: Optional[int] = None,
                 code: str = "FAL_ERROR"):
        super().__init__(message)
        self.status = status
        self.code = code


# --------------------------------------------------------------------------- #
# 成本估算
# --------------------------------------------------------------------------- #
def _image_size(path: str) -> Tuple[int, int]:
    """读图片宽高。PIL / cv2 都不可用时返回 (0, 0)，调用方需容忍。"""
    try:
        from PIL import Image
        with Image.open(path) as im:
            return int(im.width), int(im.height)
    except Exception:
        pass
    try:
        import cv2
        img = cv2.imread(path, cv2.IMREAD_COLOR)
        if img is not None:
            return int(img.shape[1]), int(img.shape[0])
    except Exception:
        pass
    return 0, 0


def clamp_scale_to_budget(in_w: int, in_h: int, scale: float,
                          max_mp: float = MAX_OUTPUT_MEGAPIXELS) -> float:
    """按输出像素预算收紧放大倍数。

    按输出 MP 计费的模型（clarity 等），成本是 factor 的平方关系：
    factor 翻倍，账单翻四倍。这里把 factor 压到预算之内。
    """
    if max_mp <= 0 or in_w <= 0 or in_h <= 0:
        return max(1.0, float(scale))
    in_mp = (in_w * in_h) / 1e6
    if in_mp <= 0:
        return max(1.0, float(scale))
    allowed = math.sqrt(max_mp / in_mp)
    return max(1.0, min(float(scale), allowed))


def estimate_cost(model: str, *, output_megapixels: float = 0.0,
                  compute_seconds: float = ESRGAN_ASSUMED_SECONDS
                  ) -> Optional[float]:
    """估算一次调用的美元成本。未知模型返回 None。"""
    info = MODEL_BILLING.get(model)
    if not info:
        return None
    unit = info.get("unit")
    price = float(info.get("price") or 0.0)
    if unit == "output_megapixel":
        return price * max(0.0, output_megapixels)
    if unit == "compute_second":
        return price * max(0.0, compute_seconds)
    return price


# --------------------------------------------------------------------------- #
# 配置读取
# --------------------------------------------------------------------------- #
def _load_dotenv_once() -> None:
    """可选加载 backend/.env。没装 python-dotenv 就静默跳过。"""
    if getattr(_load_dotenv_once, "_done", False):
        return
    _load_dotenv_once._done = True          # type: ignore[attr-defined]
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(env_path):
        return
    try:
        from dotenv import load_dotenv      # type: ignore
        load_dotenv(env_path, override=False)
    except Exception:
        # 没装 dotenv 时退化为手工解析，仍然只认 KEY=VALUE
        try:
            with open(env_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    key, value = key.strip(), value.strip().strip('"').strip("'")
                    if key and key not in os.environ:
                        os.environ[key] = value
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #
class FalImageEnhance:
    """fal.ai 双档图像增强。

    参数
    ----
    api_key : 留空则从 FAL_KEY / FAL_API_KEY 环境变量读取。
    session : 可注入 requests.Session（便于连接复用与测试）。
    """

    def __init__(self, api_key: Optional[str] = None,
                 session: Optional[requests.Session] = None,
                 max_wait: int = DEFAULT_MAX_WAIT):
        _load_dotenv_once()
        self._api_key = (api_key or "").strip() or None
        self.max_wait = max_wait
        self.session = session or requests.Session()
        # 上层每次调用后读它，记录「精细档失败已自动降级」这类提示
        self.last_notice: Optional[str] = None
        # 成本与耗时：上层据此记账、告警
        self.last_cost_usd: Optional[float] = None
        self.last_metrics: Dict[str, Any] = {}
        self.last_scale: Optional[float] = None

    # ---------------------------------------------------------------- 配置
    @property
    def api_key(self) -> str:
        if self._api_key:
            return self._api_key
        key = (os.environ.get("FAL_KEY")
               or os.environ.get("FAL_API_KEY")
               or "").strip()
        if not key:
            raise FalError(
                "未配置 fal.ai 密钥。请设置环境变量 FAL_KEY，"
                "或把 FAL_KEY=xxx:yyy 写入 backend/.env。",
                code="NOT_CONFIGURED",
            )
        return key

    @property
    def configured(self) -> bool:
        """密钥是否就绪。上层据此决定走 fal 还是本地降级。"""
        try:
            return bool(self.api_key)
        except FalError:
            return False

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": "Key " + self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # ---------------------------------------------------------------- 工具
    @staticmethod
    def _data_uri(path: str, max_bytes: int = 6 * 1024 * 1024) -> str:
        """把本地图片编码成 data URI。

        fal 的 image_url 接受这种形式，省掉一次单独的图床上传往返。
        超过 max_bytes 时先重编码成 JPEG 再传 —— 归一化输出是 PNG，
        1536 长边的照片 PNG 动辄好几 MB，base64 之后还要再涨三分之一。
        """
        if not os.path.isfile(path):
            raise FalError("待上传图片不存在：%s" % path, code="NO_INPUT")
        size = os.path.getsize(path)
        if size <= 0:
            raise FalError("待上传图片为空文件。", code="EMPTY_IMAGE")

        mime = mimetypes.guess_type(path)[0] or "image/jpeg"
        if mime not in ("image/jpeg", "image/png", "image/webp"):
            mime = "image/jpeg"

        if size > max_bytes:
            data = FalImageEnhance._shrink_to_jpeg(path)
            if data is None:
                raise FalError(
                    "图片体积 %d 字节超过上限，且压缩失败。" % size,
                    code="IMAGE_TOO_LARGE",
                )
            mime = "image/jpeg"
        else:
            with open(path, "rb") as fh:
                data = fh.read()

        return "data:%s;base64,%s" % (mime, base64.b64encode(data).decode("ascii"))

    @staticmethod
    def _shrink_to_jpeg(path: str, quality: int = 92) -> Optional[bytes]:
        """用 cv2 压成 JPEG 字节流。cv2 不可用时返回 None。"""
        try:
            import cv2  # 延迟导入：这个模块不该强依赖 OpenCV
        except Exception:
            return None
        img = cv2.imread(path, cv2.IMREAD_COLOR)
        if img is None:
            return None
        ok, buf = cv2.imencode(".jpg", img,
                               [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        return buf.tobytes() if ok else None

    @staticmethod
    def _extract_image_url(payload: Any) -> Optional[str]:
        """fal 各模型的返回结构不统一，这里做一次归一化。

        esrgan         -> {"image": {"url": ..., "content_type": ...}}
        clarity        -> {"image": {"url": ...}}
        部分模型       -> {"images": [{"url": ...}]}
        """
        if not isinstance(payload, dict):
            return None
        image = payload.get("image")
        if image is None:
            image = payload.get("images") or payload.get("output")
        if isinstance(image, list) and image:
            image = image[0]
        if isinstance(image, dict):
            url = image.get("url") or image.get("image_url")
            return url if isinstance(url, str) else None
        if isinstance(image, str):
            return image
        return None

    @staticmethod
    def _raise_for_status(resp: requests.Response, what: str) -> None:
        if resp.status_code < 400:
            return
        detail = ""
        try:
            body = resp.json()
            if isinstance(body, dict):
                err = body.get("detail") or body.get("error") or body
                detail = str(err)[:300]
            else:
                detail = str(body)[:300]
        except Exception:
            detail = (resp.text or "")[:300]
        code = "AUTH" if resp.status_code in (401, 403) else (
            "RATE_LIMIT" if resp.status_code == 429 else "HTTP_%d" % resp.status_code)
        raise FalError("%s 失败 (HTTP %d)：%s" % (what, resp.status_code, detail),
                       status=resp.status_code, code=code)

    def _request(self, method: str, url: str, *, what: str,
                 timeout: int, retries: int = 2, **kwargs) -> requests.Response:
        """带指数退避的请求。只对 5xx / 429 / 网络错误重试。"""
        last: Optional[Exception] = None
        for attempt in range(retries + 1):
            try:
                resp = self.session.request(method, url, timeout=timeout,
                                            headers=self._headers(), **kwargs)
                if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                    time.sleep(1.5 * (2 ** attempt))
                    continue
                self._raise_for_status(resp, what)
                return resp
            except FalError as exc:
                if exc.code in ("RATE_LIMIT",) or (
                        exc.status is not None and exc.status >= 500):
                    last = exc
                    if attempt < retries:
                        time.sleep(1.5 * (2 ** attempt))
                        continue
                raise
            except requests.RequestException as exc:
                last = exc
                if attempt < retries:
                    time.sleep(1.5 * (2 ** attempt))
                    continue
                raise FalError("%s 网络错误：%s" % (what, exc), code="NETWORK")
        raise FalError("%s 重试耗尽：%s" % (what, last), code="RETRY_EXHAUSTED")

    # ---------------------------------------------------------------- 队列
    def _submit(self, model: str, arguments: Dict[str, Any]) -> Tuple[str, str]:
        """提交到 fal 队列，返回 (status_url, response_url)。"""
        resp = self._request(
            "POST", "%s/%s" % (_QUEUE_BASE, model),
            what="提交任务", timeout=DEFAULT_SUBMIT_TIMEOUT, json=arguments,
        )
        data = resp.json()
        status_url = data.get("status_url")
        response_url = data.get("response_url")
        if not status_url or not response_url:
            raise FalError("fal 未返回 status_url/response_url：%s" % str(data)[:200],
                           code="BAD_RESPONSE")
        return status_url, response_url

    def _wait(self, status_url: str, response_url: str,
              deadline: float) -> Dict[str, Any]:
        """轮询直到 COMPLETED，然后取回结果体。

        fal 队列状态枚举：IN_QUEUE / IN_PROGRESS / COMPLETED。
        另外把非枚举的失败态一并当异常处理，避免死等。
        """
        while True:
            if time.time() > deadline:
                raise FalError("等待 fal 结果超时（%ds）。" % self.max_wait,
                               code="TIMEOUT")
            resp = self._request("GET", status_url, what="查询状态",
                                 timeout=DEFAULT_POLL_TIMEOUT)
            body = resp.json() or {}
            state = body.get("status", "")
            if state == "COMPLETED":
                # 按计算秒计费的模型（esrgan）靠这个字段算实际成本
                metrics = body.get("metrics")
                if isinstance(metrics, dict):
                    self.last_metrics = metrics
                break
            if state not in ("IN_QUEUE", "IN_PROGRESS", ""):
                raise FalError("fal 任务状态异常：%s" % state, code="JOB_" + str(state))
            time.sleep(DEFAULT_POLL_INTERVAL)

        resp = self._request("GET", response_url, what="读取结果",
                             timeout=DEFAULT_POLL_TIMEOUT)
        return resp.json() or {}

    def _download(self, url: str, output_path: str) -> str:
        # 结果图在 fal 的 CDN 上，不能带 Authorization 头，否则会被拒。
        try:
            resp = self.session.get(url, timeout=DEFAULT_DOWNLOAD_TIMEOUT)
        except requests.RequestException as exc:
            raise FalError("下载结果图失败：%s" % exc, code="NETWORK")
        if resp.status_code >= 400:
            raise FalError("下载结果图失败 (HTTP %d)" % resp.status_code,
                           status=resp.status_code)
        if not resp.content:
            raise FalError("下载到的结果图为空。", code="EMPTY_RESULT")
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        with open(output_path, "wb") as fh:
            fh.write(resp.content)
        return output_path

    # ---------------------------------------------------------------- 业务
    def _run(self, model: str, arguments: Dict[str, Any],
             output_path: str) -> str:
        started = time.time()
        deadline = started + self.max_wait
        status_url, response_url = self._submit(model, arguments)
        payload = self._wait(status_url, response_url, deadline)
        url = self._extract_image_url(payload)
        if not url:
            raise FalError("fal 返回里没有图片地址：%s" % str(payload)[:200],
                           code="NO_IMAGE")
        self._download(url, output_path)
        return output_path

    def enhance_light(self, input_path: str, output_path: str,
                      scale: int = 2, face: bool = False) -> str:
        """轻量档：保真超分，不生成内容。快、便宜。

        参数取自 fal-ai/esrgan 的官方 schema：
            image_url(必填), scale(1~8, 默认2), face, model, tile, output_format
        """
        actual = max(1, min(8, int(scale)))
        self._run(
            LIGHT_MODEL,
            {
                "image_url": self._data_uri(input_path),
                "scale": actual,
                "face": bool(face),
                "model": LIGHT_MODEL_ID,
                "output_format": "jpeg",
            },
            output_path,
        )
        self.last_scale = float(actual)

        # esrgan 按计算秒计费，优先用 fal 回的 inference_time，拿不到就用假设值
        seconds = ESRGAN_ASSUMED_SECONDS
        for key in ("inference_time", "inference_time_seconds", "duration"):
            value = self.last_metrics.get(key)
            if isinstance(value, (int, float)) and value > 0:
                seconds = float(value)
                break
        self.last_cost_usd = estimate_cost(LIGHT_MODEL, compute_seconds=seconds)
        return output_path

    def enhance_fine(self, input_path: str, output_path: str,
                     scale: float = 2, creativity: float = 0.25,
                     resemblance: float = 0.85,
                     num_inference_steps: int = 18,
                     guidance_scale: float = 4.0,
                     style: Optional[str] = None) -> str:
        """精细档：生成式修复。

        参数取自 fal-ai/clarity-upscaler 的官方 schema：
            image_url(必填), prompt, negative_prompt, creativity(0~1),
            resemblance(0~1), guidance_scale, num_inference_steps,
            upscale_factor, seed, enable_safety_checker

        注意字段名是 upscale_factor，不是 scale_factor；
        该模型也没有 downscaling / output_format 参数。

        creativity 越低越保守、resemblance 越高越保真 —— 修图场景不要生成。

        成本
        ----
        按「输出」像素计费 $0.03/MP，而输出像素 = 输入像素 × factor²。
        放大倍数翻倍，账单翻四倍。这里用 MAX_OUTPUT_MEGAPIXELS 把 factor
        收紧到预算之内，实际用的倍数记在 self.last_scale。

        prompt 不参与计费 —— 换提示词不涨钱，换模型和放大倍数才涨钱。
        """
        in_w, in_h = _image_size(input_path)
        asked = max(1.0, min(4.0, float(scale)))
        actual = clamp_scale_to_budget(in_w, in_h, asked)

        prompt = STYLE_PROMPTS.get(style or "", FINE_PROMPT)

        self._run(
            FINE_MODEL,
            {
                "image_url": self._data_uri(input_path),
                "prompt": prompt,
                "negative_prompt": FINE_NEGATIVE_PROMPT,
                "creativity": max(0.0, min(1.0, float(creativity))),
                "resemblance": max(0.0, min(1.0, float(resemblance))),
                "guidance_scale": float(guidance_scale),
                "num_inference_steps": max(1, int(num_inference_steps)),
                "upscale_factor": actual,
                # 输入侧安全检查保持开启；业务侧另有腾讯云内容审核兜底
                "enable_safety_checker": True,
            },
            output_path,
        )

        self.last_scale = actual
        out_mp = (((in_w * actual) * (in_h * actual)) / 1e6
                  if in_w and in_h else 0.0)
        self.last_cost_usd = estimate_cost(FINE_MODEL, output_megapixels=out_mp)
        return output_path

    def enhance(self, input_path: str, output_path: str,
                quality: str = "fine",
                style: Optional[str] = None) -> str:
        """统一入口。quality 为 light / fine。

        fine 档失败时自动退回 esrgan（至少给用户一张处理过的图），
        并把退回原因挂到 self.last_notice 上供上层记录。

        style 只影响 fine 档的提示词（不参与计费）。
        """
        self.last_notice = None
        self.last_cost_usd = None
        self.last_metrics = {}
        self.last_scale = None
        if quality == "fine":
            try:
                return self.enhance_fine(input_path, output_path, style=style)
            except FalError as exc:
                if exc.code in ("NOT_CONFIGURED", "AUTH"):
                    raise
                self.last_notice = "精细档失败(%s)，已退回轻量超分。" % exc.code
                return self.enhance_light(input_path, output_path,
                                          scale=2, face=True)
        return self.enhance_light(input_path, output_path)