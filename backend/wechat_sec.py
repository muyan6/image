# -*- coding: utf-8 -*-
"""微信小程序免费内容安全接口（mediaCheckAsync）客户端。

对比腾讯云 IMS（≈ ¥13~22/万张），微信官方的 mediaCheckAsync 对小程序
免费（有 QPS 限制），但它是**异步**接口：

    1. 服务端拿 access_token，POST media_check_async（传图片的公网 URL + 用户 openid）
       -> 立即返回 trace_id；
    2. 审核结果由微信**推回**小程序后台配置的「消息推送」URL；
    3. 我们在推送端点里解出 trace_id -> result.suggest，唤醒等待中的任务。

前提（微信公众平台 mp.weixin.qq.com 配置）：
    开发 -> 开发设置 -> 消息推送
        URL            = https://你的备案域名/api/wxpush
        Token          = 与后台「内容审核」里的推送 Token 一致
        消息加密方式   = 明文模式（本实现按明文校验签名，不引入 AES 依赖）
        数据格式       = JSON
    把该域名加进 request 合法域名并完成 ICP 备案。

图片限制：mediaCheckAsync 支持 ≤10M 的图片（jpg/jpeg/png/bmp/gif），
且要求 COS 已配置（需要给微信一个可拉的公网 URL）。
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
import uuid
from typing import Any, Dict, Optional, Tuple

import requests

from settings_store import SettingsStore

log = logging.getLogger("rescue.wechatsec")

_TOKEN_URL = "https://api.weixin.qq.com/cgi-bin/stable_token"
_CHECK_URL = "https://api.weixin.qq.com/wxa/media_check_async"
_TEXT_CHECK_URL = "https://api.weixin.qq.com/wxa/msg_sec_check"

# mediaCheckAsync 的图片大小上限
MAX_WECHAT_CHECK_BYTES = 10 * 1024 * 1024

# 异步等待结果的时间上限：微信推送一般 1~3 秒内到达
WAIT_VERDICT_SECONDS = 12.0

_token_lock = threading.Lock()
_token_cache: Dict[str, Any] = {"token": None, "expires_at": 0.0}


class WechatSecError(RuntimeError):
    def __init__(self, message: str, *, code: str = "WECHAT_SEC_ERROR") -> None:
        super().__init__(message)
        self.code = code


def wechat_sec_ready(settings: SettingsStore) -> bool:
    """微信免费审核是否可用：机审开启 + 微信密钥齐全 + COS 可用 + 推送 Token 已配。"""
    mod = settings.moderation()
    if not mod.get("enabled") or not str(mod.get("wechat_push_token") or "").strip():
        return False
    conf = settings.wechat()
    return bool(conf.get("app_id") and conf.get("app_secret")
                and settings.cos_ready())


def wechat_text_ready(settings: SettingsStore) -> bool:
    conf = settings.wechat()
    return bool(settings.moderation().get("enabled") and conf.get("app_id") and conf.get("app_secret"))


def check_text(settings: SettingsStore, content: str, openid: str) -> Tuple[str, str, int]:
    """微信 msgSecCheck v2 同步文本审核；仅用于已登录的小程序用户。"""
    try:
        resp = requests.post(_TEXT_CHECK_URL,
                             params={"access_token": get_access_token(settings)},
                             json={"content": content, "version": 2, "scene": 3, "openid": openid},
                             timeout=(10, 15))
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise WechatSecError("文字审核网络或响应错误", code="NETWORK") from exc
    if data.get("errcode"):
        raise WechatSecError("文字审核接口错误", code="API_%s" % data["errcode"])
    result = data.get("result") or {}
    if not result.get("suggest"):
        raise WechatSecError("文字审核缺少判定结果", code="BAD_RESPONSE")
    return str(result["suggest"]), str(result.get("label") or ""), 0


def get_access_token(settings: SettingsStore, force_refresh: bool = False) -> str:
    """取小程序全局 access_token（stable_token，缓存到过期前 5 分钟）。"""
    with _token_lock:
        if not force_refresh and _token_cache["token"] and time.time() < _token_cache["expires_at"]:
            return _token_cache["token"]

        conf = settings.wechat()
        appid = str(conf.get("app_id") or "").strip()
        secret = str(conf.get("app_secret") or "").strip()
        if not appid or not secret:
            raise WechatSecError("未配置微信 AppID 或 AppSecret", code="NO_CREDENTIALS")

        try:
            resp = requests.post(_TOKEN_URL, json={
                "grant_type": "client_credential",
                "appid": appid,
                "secret": secret,
                "force_refresh": force_refresh,
            }, timeout=(10, 15))
        except requests.RequestException as exc:
            raise WechatSecError("获取 access_token 网络错误: %s"
                                 % exc.__class__.__name__, code="NETWORK") from exc
        try:
            data = resp.json()
        except ValueError as exc:
            raise WechatSecError("access_token 响应不是 JSON", code="BAD_RESPONSE") from exc
        if data.get("errcode"):
            raise WechatSecError("获取 access_token 失败(%s): %s"
                                 % (data.get("errcode"), data.get("errmsg", "")),
                                 code="API_%s" % data.get("errcode"))
        token = data.get("access_token")
        if not token:
            raise WechatSecError("微信未返回 access_token", code="BAD_RESPONSE")
        _token_cache["token"] = token
        _token_cache["expires_at"] = time.time() + max(60, int(data.get("expires_in", 7200)) - 300)
        return token


def submit_image_url(settings: SettingsStore, media_url: str, openid: str) -> str:
    """Submit an existing COS signed URL; do not upload bytes or wait for callback."""
    token = get_access_token(settings)
    try:
        response = requests.post(_CHECK_URL, params={"access_token": token}, json={
            "media_url": media_url, "media_type": 2, "version": 2,
            "scene": 3, "openid": openid}, timeout=(10, 15))
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise WechatSecError("微信链接审核请求失败", code="NETWORK") from exc
    if not isinstance(data, dict) or data.get('errcode') or not data.get('trace_id'):
        raise WechatSecError("微信链接审核未返回任务标识", code="API_ERROR")
    return str(data['trace_id'])


def check_image(settings: SettingsStore, image_bytes: bytes, openid: str, cleanup_store=None
                ) -> Tuple[str, str, int]:
    """送审一张 ≤10M 的图片（异步接口同步等待）。

    返回 (suggestion, label, score)，suggestion: pass / review / risk。
    需要 COS 可用（给微信一个可拉取的签名直链）；等待超时抛 WechatSecError。
    """
    from cos_store import CosError, presign as cos_presign, put_object as cos_put

    if len(image_bytes) > MAX_WECHAT_CHECK_BYTES:
        raise WechatSecError("图片超过微信审核 10M 上限", code="TOO_LARGE")

    key = "moderation/%s/%s.jpg" % ((openid or "anon")[:8], uuid.uuid4().hex[:12])
    if cleanup_store is not None:
        cleanup_store.schedule("cos", key, time.time() + 3600)
    try:
        cos_put(settings, key, image_bytes)
        media_url = cos_presign(settings, "get", key, ttl_seconds=3600)
    except CosError as exc:
        raise WechatSecError("送审图传 COS 失败: %s" % exc, code="COS") from exc

    token = get_access_token(settings)
    try:
        try:
            resp = requests.post(_CHECK_URL, params={"access_token": token}, json={
                "media_url": media_url,
                "media_type": 2,        # 2 = 图片
                "version": 2,           # 2 = v2 版本接口（result.suggest）
                "scene": 3,             # 3 = 论坛/评论内容场景
                "openid": openid,
            }, timeout=(10, 15))
        except requests.RequestException as exc:
            raise WechatSecError("mediaCheckAsync 网络错误: %s"
                                 % exc.__class__.__name__, code="NETWORK") from exc
        try:
            data = resp.json()
        except ValueError as exc:
            raise WechatSecError("mediaCheckAsync 响应不是 JSON", code="BAD_RESPONSE") from exc
        if data.get("errcode"):
            raise WechatSecError("mediaCheckAsync 失败(%s): %s"
                                 % (data.get("errcode"), data.get("errmsg", "")),
                                 code="API_%s" % data.get("errcode"))
        wx_trace_id = str(data.get("trace_id") or "").strip()
        if not wx_trace_id:
            raise WechatSecError("微信未返回 trace_id: %s" % str(data)[:150],
                                 code="BAD_RESPONSE")

        # 用微信返回的真实 trace_id 登记等待（支持早到推送直接命中）
        pending = register_pending(wx_trace_id)
        try:
            # 微信推送通常 1~3 秒内到达；到了推送端点会 set 这个 event
            if not pending["event"].wait(WAIT_VERDICT_SECONDS):
                raise WechatSecError("等待微信审核推送超时（%ds）" % int(WAIT_VERDICT_SECONDS),
                                     code="TIMEOUT")
            return pending["verdict"]
        finally:
            discard_pending(wx_trace_id)
    except Exception:
        raise


# --------------------------------------------------------------------------- #
# 推送端点使用的登记表：trace_id -> (event, verdict)
# --------------------------------------------------------------------------- #
_pending: Dict[str, Dict[str, Any]] = {}
_early_verdicts: Dict[str, Tuple[float, Tuple[str, str, int]]] = {}
_pending_lock = threading.Lock()


def register_pending(trace_id: str) -> Dict[str, Any]:
    with _pending_lock:
        # 清理 5 分钟前残留项
        cutoff = time.time() - 300
        for tid in [k for k, v in list(_pending.items()) if v["ts"] < cutoff]:
            _pending.pop(tid, None)
        for tid in [k for k, (ts, _) in list(_early_verdicts.items()) if ts < cutoff]:
            _early_verdicts.pop(tid, None)

        entry = {
            "event": threading.Event(),
            "verdict": ("review", "timeout", 0),
            "ts": time.time()
        }
        # 如果微信推送已提前到达，直接设置结果并不再等待
        if trace_id in _early_verdicts:
            _, entry["verdict"] = _early_verdicts.pop(trace_id)
            entry["event"].set()
        _pending[trace_id] = entry
        return entry


def discard_pending(trace_id: str) -> None:
    with _pending_lock:
        _pending.pop(trace_id, None)
        _early_verdicts.pop(trace_id, None)


def resolve_pending(trace_id: str, suggestion: str, label: str, score: int) -> bool:
    """消息推送端点收到结果后调用；返回是否命中一个等待中的任务。"""
    with _pending_lock:
        entry = _pending.get(trace_id)
        if entry is None:
            # 微信推送比主线程 register 早到了几毫秒，存入早到池
            _early_verdicts[trace_id] = (time.time(), (suggestion, label, score))
            return False
        entry["verdict"] = (suggestion, label, score)
    entry["event"].set()
    return True


# --------------------------------------------------------------------------- #
# 消息推送端点的签名校验与解析（明文模式 + JSON 数据格式）
# --------------------------------------------------------------------------- #
def verify_push_signature(token: str, signature: str, timestamp: str,
                          nonce: str) -> bool:
    """微信推送签名：sha1(sort(token, timestamp, nonce))。"""
    if not (token and signature and timestamp and nonce):
        return False
    calc = hashlib.sha1("".join(sorted([token, timestamp, nonce]))
                        .encode("utf-8")).hexdigest()
    return calc == signature


def parse_push_body(body: Dict[str, Any]) -> Optional[Tuple[str, str, str, int]]:
    """从明文 JSON 推送里解出 (trace_id, suggest, label, score)。

    非审核事件（或安全模式的 Encrypt 包体）返回 None —— 本实现要求
    后台配置为「明文模式 + JSON」，安全模式需要 AES 依赖，不在零依赖范围。
    """
    if not isinstance(body, dict) or "Encrypt" in body:
        return None
    trace_id = str(body.get("trace_id") or "")
    if not trace_id:
        return None
    result = body.get("result") or {}
    if not isinstance(result, dict):
        result = {}
    suggest = str(result.get("suggest") or "error").lower()
    if body.get('errcode'):
        suggest = 'error'
    label = str(result.get("label") or 100)
    try:
        label_num = int(label)
    except (TypeError, ValueError):
        label_num = 100
    try:
        score = int(body.get("probation") or 0)
    except (TypeError, ValueError):
        score = 0
    return trace_id, suggest, str(label), score
