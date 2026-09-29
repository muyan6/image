# -*- coding: utf-8 -*-
"""
百度智能云图像增强客户端（可选后端）

两档对应百度两个接口：
    light -> image_definition_enhance   清晰度增强，约 1.5K
    fine  -> image_quality_enhance      超分辨率增强，约 2K

相比原始实现的改动
------------------
1. 恢复 TLS 证书校验。原代码用 ssl.CERT_NONE + check_hostname=False，
   等于把 access_token 明文暴露给任何能劫持链路的人。
2. access_token 刷新加锁。main.py 用线程池并发跑任务，多线程同时刷新
   会互相覆盖 token，并且多打一次无谓的请求。
3. 改用 requests.Session 复用连接；原实现每次 urlopen 都新建 TLS 握手。
4. 加入重试与明确的错误码，让 main.py 的降级链能分辨「该重试」和「该跳过」。
5. 加上百度要求的 4 MB 体积上限检查，避免上传后才被服务端拒绝。
"""
from __future__ import annotations

import base64
import logging
import os
import threading
import time
from typing import Optional

import requests

log = logging.getLogger("rescue.baidu")

__all__ = ["BaiduImageEnhance", "BaiduError"]

TOKEN_URL = "https://aip.baidubce.com/oauth/2.0/token"
API_BASE = "https://aip.baidubce.com/rest/2.0/image-process/v1"

# 档位 -> 百度接口名
ENDPOINTS = {
    "light": "image_definition_enhance",
    "fine": "image_quality_enhance",
}

# 百度要求：base64 编码前的原图不超过 4 MB
MAX_IMAGE_BYTES = 4 * 1024 * 1024

TOKEN_TIMEOUT = 15
ENHANCE_TIMEOUT = 60
RETRIES = 2

# 这些 HTTP 码值得重试（服务端临时故障）
RETRY_STATUS = (429, 500, 502, 503, 504)


class BaiduError(RuntimeError):
    """百度接口调用失败。code 供上层决定是否降级到本地引擎。"""

    def __init__(self, message: str, *, code: str = "BAIDU_ERROR",
                 status: Optional[int] = None):
        super().__init__(message)
        self.code = code
        self.status = status


class BaiduImageEnhance:
    """百度图像增强。线程安全。"""

    def __init__(self, api_key: str, secret_key: str,
                 session: Optional[requests.Session] = None):
        if not api_key or not secret_key:
            raise ValueError("百度 api_key / secret_key 不能为空")
        self.api_key = api_key
        self.secret_key = secret_key
        self.session = session or requests.Session()
        self._token: Optional[str] = None
        self._token_expires_at = 0.0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ token
    def get_token(self) -> str:
        """取可用的 access_token，过期自动刷新。多线程安全。"""
        with self._lock:
            if self._token and time.time() < self._token_expires_at:
                return self._token
            return self._refresh_locked()

    def _refresh_locked(self) -> str:
        """刷新 token。调用方必须持锁。"""
        try:
            resp = self.session.post(
                TOKEN_URL,
                params={
                    "grant_type": "client_credentials",
                    "client_id": self.api_key,
                    "client_secret": self.secret_key,
                },
                timeout=TOKEN_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise BaiduError("获取 access_token 失败：%s" % exc,
                             code="NETWORK") from exc

        if resp.status_code >= 400:
            raise BaiduError(
                "获取 access_token 失败 (HTTP %d)：%s"
                % (resp.status_code, (resp.text or "")[:200]),
                code="AUTH", status=resp.status_code,
            )

        try:
            data = resp.json()
        except ValueError as exc:
            raise BaiduError("token 响应不是 JSON：%s" % (resp.text or "")[:200],
                             code="BAD_RESPONSE") from exc

        token = data.get("access_token")
        if not token:
            raise BaiduError("百度未返回 access_token：%s" % str(data)[:200],
                             code="AUTH")

        # 提前 1 小时判过期，避开边界
        expires_in = int(data.get("expires_in", 2592000))
        self._token = token
        self._token_expires_at = time.time() + max(60, expires_in - 3600)
        log.debug("百度 access_token 已刷新，%d 秒后过期", expires_in)
        return token

    # --------------------------------------------------------------- enhance
    def enhance(self, input_path: str, output_path: str,
                quality: str = "fine") -> str:
        """执行增强。返回输出路径。

        quality == 'fine'  -> image_quality_enhance（超分，约 2K）
        quality == 'light' -> image_definition_enhance（清晰度增强，约 1.5K）
        """
        endpoint = ENDPOINTS.get(quality)
        if endpoint is None:
            raise BaiduError("未知档位：%s" % quality, code="BAD_QUALITY")

        if not os.path.isfile(input_path):
            raise BaiduError("待处理图片不存在：%s" % input_path,
                             code="NO_INPUT")
        size = os.path.getsize(input_path)
        if size <= 0:
            raise BaiduError("待处理图片为空", code="EMPTY_IMAGE")
        if size > MAX_IMAGE_BYTES:
            # 超限就压一下再传，而不是直接失败
            data = self._shrink(input_path)
            if data is None:
                raise BaiduError(
                    "图片 %d 字节超过百度 4 MB 上限，且压缩失败" % size,
                    code="IMAGE_TOO_LARGE",
                )
        else:
            with open(input_path, "rb") as fh:
                data = fh.read()

        img_b64 = base64.b64encode(data).decode("ascii")
        token = self.get_token()
        url = "%s/%s" % (API_BASE, endpoint)

        payload = {"image": img_b64}
        last_error: Optional[Exception] = None

        for attempt in range(RETRIES + 1):
            try:
                resp = self.session.post(
                    url,
                    params={"access_token": token},
                    data=payload,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    timeout=ENHANCE_TIMEOUT,
                )
            except requests.RequestException as exc:
                last_error = BaiduError("百度接口网络错误：%s" % exc, code="NETWORK")
                if attempt < RETRIES:
                    time.sleep(1.5 * (2 ** attempt))
                    continue
                raise last_error

            # token 失效：刷新一次再重试
            if resp.status_code == 401:
                with self._lock:
                    self._token_expires_at = 0.0
                token = self.get_token()
                last_error = BaiduError("access_token 失效，已刷新", code="AUTH")
                continue

            if resp.status_code in RETRY_STATUS and attempt < RETRIES:
                last_error = BaiduError(
                    "百度接口 HTTP %d" % resp.status_code,
                    code="HTTP_%d" % resp.status_code, status=resp.status_code,
                )
                time.sleep(1.5 * (2 ** attempt))
                continue

            if resp.status_code >= 400:
                raise BaiduError(
                    "百度接口 HTTP %d：%s"
                    % (resp.status_code, (resp.text or "")[:200]),
                    code="HTTP_%d" % resp.status_code, status=resp.status_code,
                )

            try:
                result = resp.json()
            except ValueError as exc:
                raise BaiduError("百度返回不是 JSON：%s" % (resp.text or "")[:200],
                                 code="BAD_RESPONSE") from exc

            if result.get("image"):
                try:
                    out_bytes = base64.b64decode(result["image"])
                except Exception as exc:  # noqa: BLE001
                    raise BaiduError("结果图 base64 解码失败：%s" % exc,
                                     code="BAD_RESPONSE") from exc
                if not out_bytes:
                    raise BaiduError("百度返回空图片", code="EMPTY_RESULT")

                os.makedirs(os.path.dirname(os.path.abspath(output_path)),
                            exist_ok=True)
                with open(output_path, "wb") as fh:
                    fh.write(out_bytes)
                return output_path

            if result.get("error_msg"):
                raise BaiduError(
                    "百度返回错误：%s (code=%s)"
                    % (result.get("error_msg"), result.get("error_code")),
                    code="API_%s" % result.get("error_code"),
                )

            raise BaiduError("未识别的百度响应：%s" % str(result)[:200],
                             code="BAD_RESPONSE")

        raise BaiduError("百度接口重试耗尽：%s" % last_error,
                         code="RETRY_EXHAUSTED")

    # ----------------------------------------------------------------- 工具
    @staticmethod
    def _shrink(path: str, quality: int = 92) -> Optional[bytes]:
        """超限时压成 JPEG 再传。cv2 不可用则返回 None。"""
        try:
            import cv2
        except Exception:
            return None
        img = cv2.imread(path, cv2.IMREAD_COLOR)
        if img is None:
            return None
        ok, buf = cv2.imencode(".jpg", img,
                               [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        return buf.tobytes() if ok else None
