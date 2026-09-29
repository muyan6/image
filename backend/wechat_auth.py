# -*- coding: utf-8 -*-
"""微信小程序登录与用户会话 token。

流程：小程序 wx.login() 拿 code -> 本模块调 code2session 换 openid
-> 用 hmac 签发自己的会话 token（12 小时），openid 永远不出后端。

token 签名密钥派生自 ADMIN_PASSWORD —— 后台改密码并重启后所有用户需重新登录，
这是有意的（省一个密钥文件）。
"""
from __future__ import annotations

import hashlib
import hmac
import re
import time
from typing import Any, Dict, Optional

import requests

from settings_store import SettingsStore

SESSION_TTL = 12 * 3600
WEB_IDENTITY_TTL = 30 * 24 * 3600
_JSCODE_URL = "https://api.weixin.qq.com/sns/jscode2session"


class WechatAuthError(RuntimeError):
    def __init__(self, message: str, *, status: Optional[int] = None,
                 code: str = "WECHAT_ERROR") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def code2session(code: str, settings: SettingsStore) -> str:
    """用 wx.login 的 code 换 openid。失败抛 WechatAuthError。"""
    conf = settings.wechat()
    app_id = (conf.get("app_id") or "").strip()
    app_secret = (conf.get("app_secret") or "").strip()
    if not app_id or not app_secret:
        raise WechatAuthError("后端未配置微信 AppID/AppSecret（后台『平台与安全』填写）",
                              code="NOT_CONFIGURED")
    try:
        resp = requests.get(_JSCODE_URL, params={
            "appid": app_id, "secret": app_secret,
            "js_code": code, "grant_type": "authorization_code",
        }, timeout=(10, 15))
    except requests.RequestException as exc:
        raise WechatAuthError("微信接口网络错误: %s" % exc.__class__.__name__,
                              code="NETWORK") from exc

    try:
        data = resp.json()
    except ValueError as exc:
        raise WechatAuthError("微信接口返回异常", code="BAD_RESPONSE") from exc
    if data.get("errcode") not in (None, 0):
        # 40029 code 无效 / 45011 频率限制 / 40226 高风险用户
        raise WechatAuthError("微信登录失败(%s): %s" % (
            data.get("errcode"), data.get("errmsg", "")),
            code="WX_%s" % data.get("errcode"))
    openid = data.get("openid")
    if not openid:
        raise WechatAuthError("微信接口未返回 openid", code="BAD_RESPONSE")
    return openid


# --------------------------------------------------------------------------- #
# 用户会话 token: "{openid}.{expiry}.{sig}"
# --------------------------------------------------------------------------- #
def _secret() -> bytes:
    from admin_api import ensure_admin_password
    password = ensure_admin_password()
    return hashlib.sha256(("rescue-user:%s" % password).encode("utf-8")).digest()


def make_token(openid: str) -> str:
    expiry = str(int(time.time()) + SESSION_TTL)
    payload = "%s|%s" % (openid, expiry)
    sig = hmac.new(_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return "%s.%s.%s" % (openid, expiry, sig)


def verify_token(token: str) -> Optional[str]:
    """校验通过返回 openid，否则 None。"""
    try:
        openid, expiry, sig = token.split(".", 2)
        payload = "%s|%s" % (openid, expiry)
        expect = hmac.new(_secret(), payload.encode("utf-8"),
                          hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expect):
            return None
        if int(expiry) < time.time():
            return None
        return openid
    except (ValueError, TypeError):
        return None


def make_web_identity(identity: str) -> str:
    expiry = str(int(time.time()) + WEB_IDENTITY_TTL)
    payload = identity + "." + expiry
    signature = hmac.new(_secret(), ("web-browser:" + payload).encode("utf-8"), hashlib.sha256).hexdigest()
    return payload + "." + signature


def verify_web_identity(token: str) -> Optional[str]:
    try:
        identity, expiry, signature = token.split(".", 2)
        if not re.fullmatch(r"web-[0-9a-f]{12,32}", identity) or int(expiry) <= time.time():
            return None
        expected = hmac.new(_secret(), ("web-browser:" + identity + "." + expiry).encode("utf-8"), hashlib.sha256).hexdigest()
        return identity if hmac.compare_digest(signature, expected) else None
    except (ValueError, TypeError, AttributeError):
        return None


def bearer_of(request_headers: Dict[str, str]) -> Optional[str]:
    auth = request_headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


def safe_user(openid: str) -> Dict[str, str]:
    """日志/审计里只露 openid 前 6 位。"""
    return openid[:6] + "***" if openid else "?"
