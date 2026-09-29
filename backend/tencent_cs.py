# -*- coding: utf-8 -*-
"""腾讯云内容安全（CMS）图片审核客户端。

纯 TC3-HMAC-SHA256 签名实现，不引入腾讯云 SDK（省一大坨依赖）。
同步接口，约 1~2 秒出结果，计费约 ¥0.0013~0.0018/张。

用法：
    suggestion, label = moderate_image_bytes(jpeg_bytes, settings)
    suggestion in ("Pass", "Review", "Block")
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import json
import logging
import time
from typing import Any, Dict, Optional, Tuple

import requests

from settings_store import SettingsStore

log = logging.getLogger("rescue.moderation")

_HOST = "cms.tencentcloudapi.com"
_SERVICE = "cms"
_VERSION = "2019-03-21"
_ACTION = "ImageModeration"
_ENDPOINT = "https://" + _HOST
# 一次调用的同步等待上限；内容审核正常 1~2 秒
_TIMEOUT = 20
# 审核失败时的重试（只重试网络/限流/服务端错误）
_MAX_ATTEMPTS = 3


class ModerationError(RuntimeError):
    def __init__(self, message: str, *, status: Optional[int] = None,
                 code: str = "MODERATION_ERROR") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def _tc3_headers(conf: Dict[str, str], payload_json: str) -> Dict[str, str]:
    """按腾讯云 TC3-HMAC-SHA256 规范生成签名请求头。"""
    secret_id = conf["secret_id"]
    secret_key = conf["secret_key"]
    ts = int(time.time())
    date = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%d")

    canonical_request = "\n".join([
        "POST",
        "/",
        "",
        "content-type:application/json; charset=utf-8\nhost:%s\nx-tc-action:%s\n" % (
            _HOST, _ACTION.lower()),
        "content-type;host;x-tc-action",
        hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
    ])
    string_to_sign = "\n".join([
        "TC3-HMAC-SHA256",
        str(ts),
        "%s/%s/tc3_request" % (date, _SERVICE),
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])

    def _hmac(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    k_date = _hmac(("TC3" + secret_key).encode("utf-8"), date)
    k_service = _hmac(k_date, _SERVICE)
    k_signing = _hmac(k_service, "tc3_request")
    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"),
                         hashlib.sha256).hexdigest()

    authorization = (
        "TC3-HMAC-SHA256 Credential=%s/%s/%s/tc3_request, "
        "SignedHeaders=content-type;host;x-tc-action, Signature=%s"
        % (secret_id, date, _SERVICE, signature))
    return {
        "Authorization": authorization,
        "Content-Type": "application/json; charset=utf-8",
        "Host": _HOST,
        "X-TC-Action": _ACTION,
        "X-TC-Version": _VERSION,
        "X-TC-Timestamp": str(ts),
    }


def moderate_image_bytes(image_bytes: bytes,
                         settings: SettingsStore) -> Tuple[str, str, int]:
    """送审一张图。返回 (suggestion, label, score)。

    suggestion: Pass / Review / Block。网络与限流自动重试；
    密钥未配置时抛 NOT_CONFIGURED，由调用方决定放行还是拦截。
    """
    conf = settings.tencent()
    if not conf.get("secret_id") or not conf.get("secret_key"):
        raise ModerationError("腾讯云密钥未配置", code="NOT_CONFIGURED")
    if len(image_bytes) > 8 * 1024 * 1024:
        # 审核接口对 base64 有体积限制，超限先压成 JPEG
        image_bytes = _squeeze(image_bytes)

    payload = json.dumps({
        "ImageBase64": base64.b64encode(image_bytes).decode("ascii"),
        "Categories": ["Porn", "Pol", "Terror"],
    }, ensure_ascii=False)
    headers = _tc3_headers(conf, payload)

    last_error: Optional[ModerationError] = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            resp = requests.post(_ENDPOINT, headers=headers,
                                 data=payload.encode("utf-8"),
                                 timeout=_TIMEOUT)
        except requests.RequestException as exc:
            last_error = ModerationError("网络错误: %s" % exc.__class__.__name__,
                                         code="NETWORK")
            time.sleep(1.5 * (attempt + 1))
            continue

        if resp.status_code in (429, 500, 502, 503, 504):
            last_error = ModerationError("HTTP %s" % resp.status_code,
                                         status=resp.status_code, code="HTTP")
            time.sleep(1.5 * (attempt + 1))
            continue
        if resp.status_code != 200:
            raise ModerationError("HTTP %s: %s" % (resp.status_code, resp.text[:150]),
                                  status=resp.status_code, code="HTTP_%s" % resp.status_code)

        try:
            body = resp.json()
        except ValueError as exc:
            raise ModerationError("返回不是 JSON: %s" % resp.text[:150],
                                  code="BAD_RESPONSE") from exc

        response = body.get("Response", {})
        if "Error" in response:
            err = response["Error"]
            code = err.get("Code", "")
            # 签名/参数类错误重试无意义，直接抛
            raise ModerationError("%s: %s" % (code, err.get("Message", "")),
                                  code="API_%s" % code)
        suggestion = response.get("Suggestion", "Pass")
        label = response.get("Label", "Normal")
        score = int(response.get("Score", 0))
        return suggestion, label, score

    raise last_error or ModerationError("重试耗尽", code="RETRY_EXHAUSTED")


def _squeeze(image_bytes: bytes, limit_mb: float = 4.0) -> bytes:
    try:
        import cv2
        import numpy as np
        arr = np.frombuffer(image_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return image_bytes
        ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
        if not ok or len(buf) > limit_mb * 1024 * 1024:
            return image_bytes
        return buf.tobytes()
    except Exception:  # noqa: BLE001
        return image_bytes
